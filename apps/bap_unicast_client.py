"""
BAP Unicast Client Class

Created on 26. Dec. 2024

@author: Markus Jellitsch
"""

from typing import Any, AsyncGenerator, Coroutine
import click
from scipy import signal
import numpy as np
import json
import contextlib
import serial as ser
from bumble.profiles.bap import (
    AudioLocation,
    SamplingFrequency,
    FrameDuration,
    CodecSpecificConfiguration,
)
from bumble.profiles.ascs import (
    AudioStreamControlServiceProxy,
    ASE_Config_Codec,
    ASE_Config_QOS,
    ASE_Disable,
    ASE_Enable,
)
from bumble.hci import CodecID, CodingFormat
from bumble.profiles.pacs import (
    PacRecord,
    PublishedAudioCapabilitiesServiceProxy,
)
from bumble.transport import open_transport
from bumble.utils import AsyncRunner
from bumble.device import (
    Device,
    Peer,
    ConnectionParametersPreferences,
    Connection,
    DeviceConfiguration,
)
from bumble.core import AdvertisingData
from bumble.snoop import BtSnooper
import functools
from bumble.profiles.ascs import AudioStreamControlServiceProxy
from bumble.hci import HCI_IsoDataPacket, HCI_LE_1M_PHY, HCI_LE_2M_PHY
from bumble import core

import logging
from leaudio import LeAudioEncoder
import asyncio
import time
import os
from bumble import hci
import sys

from leaudio import generate_sine_data, read_wav_file


DFLT_DEVICE_NAME = "Bumble Unicast Client"
DFLT_DEVICE_ADDRESS = hci.Address("F0:F1:F2:F3:F4:F5")


class BapUnicastClient(Device.Listener):
    """BAP Unicast Client Class"""

    dflt_target_name = "BUMBLE SINK"

    def __init__(self, device):
        """
        Initialize the BAP Unicast Client.

        Args:
            device: The device that this client is running on.

        Attributes:
            target_name: The name of the target device to connect to.
            send_complete: Whether the ISO data has been fully sent.
            packet_sequence_number: The sequence number of the current ISO packet.
            iso_packets: The list of ISO packets to send.
            codec_config: The codec specific configuration.
        """

        # additional device attributes
        self.device = device
        self.device.listener = self
        self.device.cis_enabled = True

        self.target_name = self.dflt_target_name
        self.send_complete = asyncio.Event()
        self.stream_started = asyncio.Event()
        self.packet_sequence_number = 0
        self.connection = None
        self.iso_packets = []
        self.codec_config = CodecSpecificConfiguration(
            sampling_frequency=SamplingFrequency.FREQ_24000,
            frame_duration=FrameDuration.DURATION_10000_US,
            audio_channel_allocation=AudioLocation.FRONT_RIGHT,
            octets_per_codec_frame=60,
            codec_frames_per_sdu=1,
        )


    @AsyncRunner.run_in_task()
    async def on_advertisement(self, advertisement):
        # find the target device
        """
        This method is called when a BAP advertisement is received.

        It checks if the advertisement is from the target device by comparing the
        complete local name in the advertisement data with the target name. If the
        names match, it stops scanning and establishes a connection to the target
        device.

        Args:
            advertisement: The advertisement received.
        """

        def parse_ltv_packet(data):
            packets = []

            i = 0
            while i < len(data) - 1:
                length_byte = data[i]
                type_byte = data[i + 1]
                value_bytes = data[i + 2 : i + length_byte + 1]
                packets.append((length_byte, type_byte, value_bytes))
                i += length_byte + 1

            return packets

        ltv_packets = parse_ltv_packet(advertisement.data_bytes)
        for _, type, values in ltv_packets:
            if type is AdvertisingData.COMPLETE_LOCAL_NAME:
                to_compare = bytes(self.target_name, "utf-8")

                if values == to_compare:
                    logging.info("found complete local name:" + self.target_name)

                    await self.device.stop_scanning()
                    params = ConnectionParametersPreferences()

                    params.connection_interval_min = 47
                    params.connection_interval_max = 47

                    prefs = {HCI_LE_1M_PHY: params, HCI_LE_2M_PHY: params}

                    await self.device.connect(
                        peer_address=advertisement.address,
                        connection_parameters_preferences=prefs,
                    )

    @AsyncRunner.run_in_task()
    async def on_connection(self, connection: Connection):
        """
        This method is called when a connection is established to the target device
        :param connection: the established connection
        :return: nothing
        """

        notifications = {1: asyncio.Queue()}

        def on_notification(data: bytes, ase_id: int):
            notifications[ase_id].put_nowait(data)

        self.connection = connection
        peer = Peer(connection)
        logging.info(f"connection established => address: {connection.peer_address}")

        await connection.pair()
        logging.info("Link encrypted")

        await connection.set_phy(rx_phys=[HCI_LE_2M_PHY], tx_phys=[HCI_LE_2M_PHY])
        logging.info("PHY updated")

        await peer.request_mtu(1691)
        logging.info("MTU requested")

        remote_features = await self.device.get_remote_le_features(connection)
        logging.info(f"Remote features: {remote_features}")

        pacs_client = await peer.discover_service_and_create_proxy(
            PublishedAudioCapabilitiesServiceProxy
        )
        ascs_client = await peer.discover_service_and_create_proxy(
            AudioStreamControlServiceProxy
        )

        response = await pacs_client.sink_pac.read_value()
        pac_record = PacRecord.from_bytes(response[1:])
        logging.info(f"Sink PAC: {pac_record.codec_specific_capabilities}")

        await ascs_client.ase_control_point.subscribe()
        await ascs_client.sink_ase[0].subscribe(
            functools.partial(on_notification, ase_id=1)
        )

        await ascs_client.ase_control_point.write_value(
            ASE_Config_Codec(
                ase_id=[1],
                target_latency=[0x3],
                target_phy=[2],
                codec_id=[CodingFormat(CodecID.LC3)],
                codec_specific_configuration=[self.codec_config],
            )
        )

        # wait for notification
        ase_state = await notifications[1].get()
        logging.info(f"Codec Configure =>ASE state: {ase_state}")

        cis_handles = await self.device.setup_cig(
            cig_id=1,
            cis_id=[1],
            sdu_interval=(
                self.codec_config.frame_duration.us,
                self.codec_config.frame_duration.us,
            ),
            framing=0,
            max_sdu=(self.codec_config.octets_per_codec_frame, 0),
            retransmission_number=15,
            max_transport_latency=(95, 95),
        )
        logging.info(f"CIG Setuo =>CIS handles: {cis_handles}")

        await ascs_client.ase_control_point.write_value(
            ASE_Config_QOS(
                ase_id=[1],
                cig_id=[1],
                cis_id=[1],
                sdu_interval=[self.codec_config.frame_duration.us],
                framing=[0],
                phy=[2],
                max_sdu=[self.codec_config.octets_per_codec_frame],
                retransmission_number=[15],
                max_transport_latency=[95],
                presentation_delay=[40000],
            )
        )

        ase_state = await notifications[1].get()
        logging.info(f"QoS Configure =>ASE state: {ase_state}")

        await ascs_client.ase_control_point.write_value(
            ASE_Enable(
                ase_id=[1],
                metadata=[bytes([0x03, 0x02, 0x01, 0x00])],
            )
        )

        ase_state = await notifications[1].get()
        logging.info(f"Enable =>ASE state: {ase_state}")

        await self.device.create_cis([(cis_handles[0], connection.handle)])

        await self.device.send_command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                connection_handle=cis_handles[0],
                data_path_direction=hci.HCI_LE_Setup_ISO_Data_Path_Command.Direction.HOST_TO_CONTROLLER,
                data_path_id=0x00,  # Fixed HCI
                codec_id=hci.CodingFormat(hci.CodecID.TRANSPARENT),
                controller_delay=0,
                codec_configuration=b"",
            )
        )

        ase_state = await notifications[1].get()
        logging.info(f"Enabled =>ASE state: {ase_state}")

        self.packet_sequence_number = 0
        self.iso_packet = HCI_IsoDataPacket(
            connection_handle=cis_handles[0],
            data_total_length=self.codec_config.octets_per_codec_frame + 4,
            packet_sequence_number=self.packet_sequence_number,
            pb_flag=0b10,
            packet_status_flag=0,
            iso_sdu_length=self.codec_config.octets_per_codec_frame,
            iso_sdu_fragment=bytes([0] * self.codec_config.octets_per_codec_frame),
        )

        self.stream_started.set()
        self.device.host.on("packet_complete", self.on_iso_pdu_sent)
        self.device.host.send_hci_packet(self.iso_packet)

        logging.info("Audio Setup complete. Sending ISO packets...")


    def on_iso_pdu_sent(self, event):
        """
        This method is called when an ISO PDU has been sent.

        It sends the next ISO packet if the packet sequence number is less than the
        number of ISO packets to send. If the packet sequence number is equal to the
        number of ISO packets to send, it sets the send_complete flag to True.

        Args:
            event: The event passed to this method from the HCI host.
        """
        if self.packet_sequence_number < len(self.iso_packets) - 1:
            # send the next ISO packet
            self.packet_sequence_number += 1
            self.iso_packet.packet_sequence_number = self.packet_sequence_number
            self.iso_packet.iso_sdu_fragment = self.iso_packets[
                self.packet_sequence_number
            ]
            self.device.host.send_hci_packet(self.iso_packet)
        else:
            self.send_complete.set()

    def set_iso_data(self, iso_packets):
        """
        Set the ISO packets to be sent.

        Args:
            iso_packets: A list of ISO packets to be sent.
        """

        self.iso_packets = iso_packets

    async def start_streaming(self, target_name, codec_config):
        """
        Start streaming the ISO data to the target device.

        Args:
            target_name: The name of the target device to connect to.
            codec_config: The codec specific configuration.
        """
        self.target_name = target_name
        self.codec_config = codec_config
        await self.device.power_on()
        await self.device.start_scanning()
        logging.info(f"Start connecting to target device {target_name}")

    async def stop_streaming(self):
        """
        Stop streaming the ISO data.

        This method stops the streaming by disconnecting the connection and powering off the device.
        """
        if self.connection:
            await self.connection.disconnect()
            logging.info("Streaming stopped")
        await self.device.power_off()
        logging.info("Power off device")
        
    
    async def wait_for_streaming(self, timeout: float):
        try:
            await asyncio.wait_for(self.stream_started.wait(), timeout)
            logging.info("Streaming started")
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError from error
    async def wait_for_complete(self, timeout: float):
        try:
            await asyncio.wait_for(self.send_complete.wait(), timeout)
            logging.info("Streaming complete")
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError from error



@contextlib.asynccontextmanager
async def create_device(transport: str) -> AsyncGenerator[Device, Any]:
    transport = "serial:" + transport
    async with await open_transport(transport) as (
        hci_source,
        hci_sink,
    ):
        device_config = DeviceConfiguration(
            name=DFLT_DEVICE_NAME,
            address=DFLT_DEVICE_ADDRESS,
            keystore="JsonKeyStore",
        )

        device = Device.from_config_with_hci(
            device_config,
            hci_source,
            hci_sink,
        )

        yield device


def clean_reset(com_port):
    """
    Send the HCI_RESET command (0x03) to the given serial port
    This is used to put the nRF53 DK into a known state before running
    the test suite. The command is sent twice with a half second delay
    in between to ensure that the device is properly reset.
    """
    try:
        with ser.Serial(com_port, baudrate=1000000, timeout=1) as s:
            # Send the HCI_RESET command (0x03)
            s.write(bytes(hci.HCI_Reset_Command()))
            time.sleep(0.5)
            s.write(bytes(hci.HCI_Reset_Command()))

    except Exception as e:
        logging.error(f"Error opening or accessing {com_port}: {e}")


def run_async(async_command: Coroutine) -> None:
    try:
        asyncio.run(async_command)
    except core.ProtocolError as error:
        logging.error(f"Protocol error: {error}")
        sys.exit(1)


async def run_unicast(
    port, wav, target_name, sample_rate, sine_frequency, duration_s, verbose
) -> None:
    if verbose > 0:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    # clean controller reset
    clean_reset(port)

    async with create_device(port) as device:
        # create snoop file
        f = open("log.btsnoop", "wb")
        Snooper = BtSnooper(f)
        device.host.snooper = Snooper

        codec_config = CodecSpecificConfiguration(
            sampling_frequency=sample_rate,
            frame_duration=FrameDuration.DURATION_10000_US,
            audio_channel_allocation=AudioLocation.FRONT_RIGHT,
            octets_per_codec_frame=60,
            codec_frames_per_sdu=1,
        )

        # prepare the samples
        frame_size = int(
            codec_config.sampling_frequency.hz
            * codec_config.frame_duration.us
            / 1000
            / 1000
        )
        if wav is not None:
            test_raw_data = read_wav_file(wav, codec_config.sampling_frequency.hz)
            frame_num = int(len(test_raw_data) // frame_size)

        else:
            test_raw_data = generate_sine_data(
                sine_frequency, codec_config.sampling_frequency.hz, duration=duration_s
            )
            frame_num = int(1000000 / codec_config.frame_duration.us * duration_s)

        # setup the LC3 encoder
        encoder = LeAudioEncoder()
        encoder.setup_encoders(
            codec_config.sampling_frequency.hz,
            codec_config.frame_duration.us,
            1,
        )

        iso_packets = []
        for i in range(frame_num):
            pcm_data = test_raw_data[i * frame_size : i * frame_size + frame_size]
            data = encoder.encode(
                codec_config.octets_per_codec_frame, 1, 1, bytes(pcm_data)
            )
            iso_packets.append(data)

        try:
            logging.info(f"Use Serial Port: {port}")
            logging.info(f"Use Target Name: {target_name}")
            logging.info(f"Use Sample Rate: {sample_rate.hz} Hz")
            logging.info(f"Use Frame Duration: {codec_config.frame_duration.us} us")
            logging.info(
                f"Use Octets per Codec Frame: {codec_config.octets_per_codec_frame} bytes"
            )
            logging.info(
                f"Use Codec Frames per SDU: {codec_config.codec_frames_per_sdu}"
            )

            if wav is not None:
                logging.info(f"Use Wav File: {wav}")
                logging.info(f"Use Duration: {frame_num / 100} s")
            else:
                logging.info(f"Use Sine Frequency: {sine_frequency} Hz")
                logging.info(f"Use Duration: {frame_num / 100} s")

            bap_client = BapUnicastClient(device=device)
            bap_client.set_iso_data(iso_packets)
            await bap_client.start_streaming(target_name, codec_config)
            await bap_client.wait_for_streaming(15)
            await bap_client.wait_for_complete(100)
            logging.info("Streaming complete")
        except asyncio.TimeoutError:
            logging.error("Timeout waiting for streaming to start/complete")
        finally:
            await bap_client.stop_streaming()
            logging.info("Bye!!!")


@click.command()
@click.argument(
    "port",
    metavar="com port",
    type=str,
)
@click.option(
    "--wav",
    metavar="WAV_FILE_PATH",
    type=str,
    help="Wav file path",
)
@click.option(
    "--target-name",
    metavar="TARGET_NAME",
    type=str,
    default="BUMBLE",
    help="Target name",
)
@click.option(
    "--sample-rate",
    metavar="SAMPLE_RATE",
    type=click.Choice(
        [
            str(SamplingFrequency.FREQ_16000.value),
            str(SamplingFrequency.FREQ_24000.value),
            str(SamplingFrequency.FREQ_48000.value),
        ]
    ),
    default=str(SamplingFrequency.FREQ_24000.value),
    help="Sample rate (0: 16000, 1: 24000, 2: 48000)",
)
@click.option(
    "--sine-frequency",
    metavar="SINEFREQUENCY",
    type=int,
    default=1000,
    help="Frequency of sine wave if no wav file is provided",
)
@click.option(
    "--duration",
    metavar="DURATION",
    type=int,
    default=10,
    help="Duration in seconds of the sine wave if no wav file is provided",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable verbose logging",
)
def main(port, wav, target_name, sample_rate, sine_frequency, duration, verbose):
    """Start unicast client"""
    # ctx.ensure_object(dict)

    run_async(
        run_unicast(
            port=port,
            wav=wav,
            target_name=target_name,
            sample_rate=SamplingFrequency(int(sample_rate)),
            sine_frequency=sine_frequency,
            duration_s=duration,
            verbose=verbose,
        )
    )


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
