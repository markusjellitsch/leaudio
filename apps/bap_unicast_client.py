"""
BAP Unicast Client Application

Created on 26. Dec. 2024

@author: Markus Jellitsch
"""

from typing import Coroutine
import click
from scipy import signal
import numpy as np
import argparse
import json
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
from bumble.transport import serial
from bumble.utils import AsyncRunner
from bumble.device import Device, Peer, ConnectionParametersPreferences, Connection
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


app_specific_codec = CodecSpecificConfiguration(
    sampling_frequency=SamplingFrequency.FREQ_24000,
    frame_duration=FrameDuration.DURATION_10000_US,
    audio_channel_allocation=AudioLocation.FRONT_RIGHT,
    octets_per_codec_frame=60,
    codec_frames_per_sdu=1,
)

TEST_SINE = 1

complete_local_name = "BUMBLE"
iso_packets = []
upsampled_left_channel = None


def clean_reset(com_port):
    try:
        with ser.Serial(com_port, baudrate=1000000, timeout=1) as s:
            # Send the HCI_RESET command (0x03)
            s.write(bytes(hci.HCI_Reset_Command()))
            time.sleep(0.5)
            s.write(bytes(hci.HCI_Reset_Command()))
            print(f"HCI_RESET command sent to {com_port}")

    except Exception as e:
        print(f"Error opening or accessing {com_port}: {e}")






class Listener(Device.Listener):

    packet_sequence_number = 0

    def __init__(self, device):

        self.device = device

    @AsyncRunner.run_in_task()
    async def on_advertisement(self, advertisement):

        def parse_ltv_packet(data):

            packets = []

            i = 0

            while i < len(data)-1:

                length_byte = data[i]

                type_byte = data[i + 1]

                value_bytes = data[i+2:i+length_byte+1]

                packets.append((length_byte, type_byte, value_bytes))

                i += length_byte+1

            return packets

        ltv_packets = parse_ltv_packet(advertisement.data_bytes)

        for _, type, values in ltv_packets:

            if type is AdvertisingData.COMPLETE_LOCAL_NAME:

                to_compare = bytes(complete_local_name, "utf-8")

                if values == to_compare:

                    print("found device " + complete_local_name)

                    await self.device.stop_scanning()

                    params = ConnectionParametersPreferences()

                    params.connection_interval_min = (47)

                    params.connection_interval_max = (47)

                    prefs = {HCI_LE_1M_PHY: params, HCI_LE_2M_PHY: params}

                    await self.device.connect(peer_address=advertisement.address, connection_parameters_preferences=prefs)

    @AsyncRunner.run_in_task()
    async def on_connection(self, connection: Connection):

        notifications = {1: asyncio.Queue()}

        def on_notification(data: bytes, ase_id: int):

            notifications[ase_id].put_nowait(data)

        print(f'=== Connected to {connection}')

        peer = Peer(connection)

        # set PHY to 2M
        await connection.set_phy(rx_phys=[HCI_LE_2M_PHY], tx_phys=[HCI_LE_2M_PHY])

        # pair with the device
        await connection.pair()

        # request mtu change
        mtu = await peer.request_mtu(1691)

        # get remote features
        remote_features = await self.device.get_remote_le_features(connection)

        print(f"peer supports the following features:{remote_features}")

        # discover services
        pacs_client = await peer.discover_service_and_create_proxy(PublishedAudioCapabilitiesServiceProxy)
        ascs_client = await peer.discover_service_and_create_proxy(AudioStreamControlServiceProxy)

        # read sink PACs
        response = await pacs_client.sink_pac.read_value()
        pac_record = PacRecord.from_bytes(response[1:])
        print(pac_record)

        # enable ASCS notifications
        await ascs_client.ase_control_point.subscribe()
        await ascs_client.sink_ase[0].subscribe(
            functools.partial(on_notification, ase_id=1)
        )

        # read sink ASE state
        sink_state = await ascs_client.sink_ase[0].read_value()
        print(sink_state)

        print(app_specific_codec)
        await ascs_client.ase_control_point.write_value(
            ASE_Config_Codec(
                ase_id=[1],
                target_latency=[0x3],
                target_phy=[2],
                codec_id=[CodingFormat(CodecID.LC3)],
                codec_specific_configuration=[app_specific_codec],
            )


        )

        # wait for notification
        await notifications[1].get()
        print("ASE: codec configured")

        # setup the CIG
        cis_handles = await self.device.setup_cig(
            cig_id=1,
            cis_id=[1],
            sdu_interval=(app_specific_codec.frame_duration.us,
                          app_specific_codec.frame_duration.us),
            framing=0,
            max_sdu=(app_specific_codec.octets_per_codec_frame, 0),
            retransmission_number=15,
            max_transport_latency=(95, 95),
        )

        # configure  ASE (config QOS)
        await ascs_client.ase_control_point.write_value(

            ASE_Config_QOS(
                ase_id=[1],
                cig_id=[1],
                cis_id=[1],
                sdu_interval=[app_specific_codec.frame_duration.us],
                framing=[0],
                phy=[2],
                max_sdu=[app_specific_codec.octets_per_codec_frame],
                retransmission_number=[15],
                max_transport_latency=[95],
                presentation_delay=[40000],
            )
        )

        # wait for notifications
        await notifications[1].get()
        print("ASE: QOS configured")

        # configure ASE (Enable)
        await ascs_client.ase_control_point.write_value(
            ASE_Enable(
                ase_id=[1],
                # neeeded?
                metadata=[bytes([0x03, 0x02, 0x01, 0x00])],
            )
        )

        # wait for notifications
        await notifications[1].get()
        print('ASE: enabling')

        # create CIS
        await self.device.create_cis(
            [
                (cis_handles[0], connection.handle)
            ]
        )

        print('ASE: cis established')

        await self.device.send_command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                connection_handle=cis_handles[0],
                data_path_direction=hci.HCI_LE_Setup_ISO_Data_Path_Command.Direction.HOST_TO_CONTROLLER,
                data_path_id=0x00,  # Fixed HCI
                codec_id=hci.CodingFormat(hci.CodecID.TRANSPARENT),
                controller_delay=0,
                codec_configuration=b'',
            ))

        # wait for notifications
        await notifications[1].get()
        print('ASE: audio stream enabled')

        # prepere the ISO packets
        self.packet_sequence_number = 0
        self.iso_packet = HCI_IsoDataPacket(
            connection_handle=cis_handles[0],
            data_total_length=app_specific_codec.octets_per_codec_frame + 4,
            packet_sequence_number=self.packet_sequence_number,
            pb_flag=0b10,
            packet_status_flag=0,
            iso_sdu_length=app_specific_codec.octets_per_codec_frame,
            iso_sdu_fragment=bytes(
                [0]*app_specific_codec.octets_per_codec_frame),
        )

        self.send_complete = False

        def on_iso_pdu_sent(event):
            if self.packet_sequence_number < len(iso_packets) - 1:
                # send the next ISO packet
                self.packet_sequence_number += 1
                self.iso_packet.packet_sequence_number = self.packet_sequence_number
                self.iso_packet.iso_sdu_fragment = iso_packets[self.packet_sequence_number]
                self.device.host.send_hci_packet(self.iso_packet)
            else:
                self.send_complete = True

        self.device.host.on('packet_complete', on_iso_pdu_sent)
        self.device.host.send_hci_packet(self.iso_packet)

        while True:
            await asyncio.sleep(1)
            if self.send_complete:
                print("send complete. bye bye")
                await self.device.power_off()
                self.device.future.set_result(None)
                break


def run_async(async_command: Coroutine) -> None:
    try:
        asyncio.run(async_command)
    except core.ProtocolError as error:
        print(f"Protocol error: {error}")
        sys.exit(1)

@click.command()
@click.argument(
    'port',
    metavar='com port',
    type=str,
)
@click.option(
    '--wav',
    metavar='WAV_FILE_PATH',
    type=str,
    help='Wav file path',)
@click.option(
    '--target-name',
    metavar='TARGET_NAME',
    type=str,
    default="BUMBLE",
    help='Target name'
)
@click.option(
    '--sample-rate',
    metavar='SAMPLE_RATE',
    type=int,
    help='Sample rate',
)
@click.option(
    '--verbose',
    '-v',
    is_flag=True,
    default=False,
    help='Enable verbose logging',
)
def unicast(port,wav,target_name,sample_rate,verbose):
    """Start unicast client """
    # ctx.ensure_object(dict)
    run_async(
        run_unicast(
           port=port,
           wav=wav,
           target_name=target_name,
           sample_rate=sample_rate,
           verbose=verbose
        )
    )

async def run_unicast(port,wav,target_name,sample_rate,verbose) -> None:
    global complete_local_name

    if verbose > 0:
        logging.basicConfig(level=logging.DEBUG)
    else:

        logging.basicConfig(level=logging.ERROR)

    print("sample rate", sample_rate)
    if not sample_rate or sample_rate == 0:
        app_specific_codec.sampling_frequency = SamplingFrequency.FREQ_16000
        app_specific_codec.frame_duration = FrameDuration.DURATION_10000_US
        app_specific_codec.octets_per_codec_frame = 40
    elif sample_rate == 1:
        app_specific_codec.sampling_frequency = SamplingFrequency.FREQ_24000
        app_specific_codec.frame_duration = FrameDuration.DURATION_10000_US
        app_specific_codec.octets_per_codec_frame = 60
    elif sample_rate == 2:
        app_specific_codec.sampling_frequency = SamplingFrequency.FREQ_48000
        app_specific_codec.frame_duration = FrameDuration.DURATION_10000_US
        app_specific_codec.octets_per_codec_frame = 120
    else:
        raise ValueError("unknown sample rate")

    # Define the data as a Python dictionary
    config_data = {
        "name": "Unicast Client",
        "address": "C0:98:E5:49:00:00"
    }

    # Write the data to a JSON file
    dev_config = "device.json"
    with open(dev_config, "w") as outfile:
        # Indent for better readability
        json.dump(config_data, outfile, indent=4)

    print(f"sample rate: {app_specific_codec.sampling_frequency.hz} Hz")
    print(f"frame duration: {app_specific_codec.frame_duration.us}  us")
    print(f"octets per codec frame: {app_specific_codec.octets_per_codec_frame} bytes")

    if wav:
        sound_file = wav
        TEST_SINE = 0
    else:
        TEST_SINE = 1
        print("Test Sine is used")

    if target_name:
        complete_local_name = target_name
        print(f"Target: {complete_local_name}")

    # clean controller reset
    clean_reset(port)

    # open the transport
    hci_transport = await serial.open_serial_transport(port)

    # create a device to manage the host, with a custom listener
    device = Device.from_config_file_with_hci(
        dev_config, hci_transport.source, hci_transport.sink
    )

    device.listener = Listener(device)
    device.cis_enabled = True

    # create snoop file
    f = open("log.btsnoop", "wb")
    Snooper = BtSnooper(f)
    device.host.snooper = Snooper

    # setup the LC3 encoder
    encoder = LeAudioEncoder()
    encoder.setup_encoders(
        app_specific_codec.sampling_frequency.hz,
        app_specific_codec.frame_duration.us,
        1,
    )

    num_runs = 0

    # prepare the samples
    # calculate the number of samples per frame duration.
    sample_size = int(app_specific_codec.sampling_frequency.hz *
                      app_specific_codec.frame_duration.us / 1000 / 1000)
    if TEST_SINE == 0:
        if os.path.isfile(sound_file):
            upsampled_left_channel = read_wav_file(sound_file,app_specific_codec.sampling_frequency.hz)
        else:
            raise FileNotFoundError(f"The file {sound_file} does not exist.")

        num_runs = len(upsampled_left_channel) // sample_size
    else:
        num_runs = 2000

    for i in range(num_runs):

        if TEST_SINE == 0:
            pcm_data = upsampled_left_channel[i *
                                              sample_size:i*sample_size+sample_size]
        else:
            pcm_data = generate_sine_data(
                1000, app_specific_codec.sampling_frequency.hz, app_specific_codec.frame_duration.us / 1000000)

        data = encoder.encode(
            app_specific_codec.octets_per_codec_frame, 1, 1, bytes(pcm_data))
        iso_packets.append(data)
    print("encoding finished. num_iso_packets:", len(iso_packets))

    print("power on ...")
    await device.power_on()

    print(f'start scanning ...')
    await device.start_scanning(scanning_phys=[HCI_LE_1M_PHY], legacy=False)

    device.future = asyncio.get_running_loop().create_future()
    await device.future
    print("all done")

def main():
    asyncio.run(unicast())
