"""
BAP Unicast Client Class

Created on 26. Dec. 2024

@author: Markus Jellitsch
"""

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
from bumble.profiles.bap import (
    AudioLocation,
    SamplingFrequency,
    FrameDuration,
    CodecSpecificConfiguration,
)
from bumble.profiles.ascs import (
    ASE_Config_Codec,
    ASE_Config_QOS,
    ASE_Enable,
)
from bumble.hci import CodecID, CodingFormat
from bumble.profiles.pacs import (
    PacRecord,
)
from bumble.utils import AsyncRunner
from bumble.device import (
    Connection,
)
import functools
from bumble.hci import HCI_IsoDataPacket
import logging
import asyncio
from bumble import hci

from leaudio.bap_connector import BapConnector


class BapUnicastClient(BapConnector):
    """BAP Unicast Client Class"""

    def __init__(self, device):
        """
        Initialize the BAP Unicast Client.

        Args:
            device: The device that this client is running on.

        Attributes:
            send_complete: Whether the ISO data has been fully sent.
            packet_sequence_number: The sequence number of the current ISO packet.
            iso_packets: The list of ISO packets to send.
            codec_config: The codec specific configuration.
        """

        super().__init__(device)
        self.send_complete = asyncio.Event()
        self.packet_sequence_number = 0
        self.iso_packets = []
        self.codec_config = CodecSpecificConfiguration(
            sampling_frequency=SamplingFrequency.FREQ_24000,
            frame_duration=FrameDuration.DURATION_10000_US,
            audio_channel_allocation=AudioLocation.FRONT_RIGHT,
            octets_per_codec_frame=60,
            codec_frames_per_sdu=1,
        )


    async def _start_streaming(self, codec_config):

        """
        Start streaming ISO data to a unicast target.

        This method sets up and configures the unicast streaming session by
        subscribing to notifications, configuring the codec, setting up the CIG
        (Connected Isochronous Group), configuring QoS (Quality of Service), enabling
        the ASE (Audio Stream Endpoint), and establishing the CIS (Connected
        Isochronous Stream). It also configures the ISO data path and begins sending
        ISO packets.

        Args:
            target_name: The name of the target device to stream audio to.
            codec_config: The codec specific configuration used for the stream.

        """

        self.codec_config = codec_config
        notifications = {1: asyncio.Queue()}

        def on_notification(data: bytes, ase_id: int):
            notifications[ase_id].put_nowait(data)

        response = await self.pacs_client.sink_pac.read_value()
        pac_record = PacRecord.from_bytes(response[1:])
        logging.info(f"Sink PAC: {pac_record.codec_specific_capabilities}")

        await self.ascs_client.ase_control_point.subscribe()
        await self.ascs_client.sink_ase[0].subscribe(
            functools.partial(on_notification, ase_id=1)
        )

        await self.ascs_client.ase_control_point.write_value(
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

        await self.ascs_client.ase_control_point.write_value(
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

        await self.ascs_client.ase_control_point.write_value(
            ASE_Enable(
                ase_id=[1],
                metadata=[bytes([0x03, 0x02, 0x01, 0x00])],
            )
        )

        ase_state = await notifications[1].get()
        logging.info(f"Enable =>ASE state: {ase_state}")

        await self.device.create_cis([(cis_handles[0], self.peer.connection.handle)])

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

    async def stop_streaming(self):
        """
        Stop streaming the ISO data.

        This method stops the streaming by disconnecting the connection and powering off the device.
        """

        await self.disconnect()
        logging.info("Streaming stopped")
        await self.device.power_off()
        logging.info("Power off device")

    async def start_streaming(self,codec_config, timeout=10):
        try:
            await asyncio.wait_for(self._start_streaming(codec_config), timeout)
            logging.info("Streaming started")
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError from error

    async def wait_for_complete(self, timeout: float):
        try:
            await asyncio.wait_for(self.send_complete.wait(), timeout)
            logging.info("Streaming complete")
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError from error
