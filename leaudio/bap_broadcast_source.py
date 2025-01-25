"""
BAP Broadcast Source Application
(based on Googe Bumble's Auracast example)

Created on 26. Dec. 2024

@author: Markus Jellitsch
"""

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
from __future__ import annotations
import asyncio
import logging
from bumble import core
from bumble import hci
from bumble.profiles import bap
from bumble.profiles import le_audio
import bumble.device
import bumble.transport
import bumble.utils

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)


class BapBroadcastSource:
    def __init__(self, device):
        """
        Initialize the BAP Broadcast Source.

        Args:
            device: The device that this broadcast source is running on.

        Attributes:
            bis_link: The BIS link to send the ISO packets.
            iso_packets: The list of ISO packets to send.
            packet_sequence_number: The sequence number of the current ISO packet.
            stream_started: An event that is set when the stream has started.
            stream_complete: An event that is set when the stream has completed.
            repeat: Whether to repeat the stream.
        """
        self.device = device
        self.device.on("packet_complete", self.on_iso_pdu_sent)
        self.bis_link = None
        self.iso_packets = []
        self.packet_sequence_number = 0
        self.stream_started = asyncio.Event()
        self.send_complete = asyncio.Event()
        self.repeat = False

    def on_iso_pdu_sent(self, event):
        """
        Handle the event when an ISO PDU has been sent.

        This method writes the next ISO packet to the BIS link and increments the
        packet sequence number. If the sequence number equals the length of the ISO
        packets, it either resets the sequence number if repeating is enabled or
        sets the stream_complete event.

        Args:
            event: The event triggered after sending an ISO PDU.
        """

        self.bis_link.write(self.iso_packets[self.packet_sequence_number])
        self.packet_sequence_number += 1
        if self.packet_sequence_number == len(self.iso_packets):
            if self.repeat:
                self.packet_sequence_number = 0
            else:
                self.send_complete.set()

    def set_iso_data(self, iso_packets):
        """
        Set the ISO packets to be sent.

        Args:
            iso_packets: A list of ISO packets to be sent.
        """

        self.iso_packets = iso_packets

    async def start_streaming(
        self, broadcast_id: int, broadcast_code, codec_config, repeat
    ):
        """
        Start streaming the ISO data as a broadcast source.

        This method initializes the broadcast source by powering on the device, setting
        up codec configuration, and creating advertising and BIG (Broadcast Isochronous
        Group) for streaming. It also sets up the data path for sending ISO packets and
        begins sending frames.

        Args:
            broadcast_id (int): The unique ID for the broadcast.
            broadcast_code: The optional code for broadcast encryption.
            codec_config: The codec specific configuration used for the stream.
            repeat (bool): Whether to repeat the stream after completion.
        """

        await self.device.power_on()

        self.codec_config = codec_config
        self.repeat = repeat

        logger.info("Create Avertising Data")
        broadcast_audio_announcement = bap.BroadcastAudioAnnouncement(broadcast_id)
        basic_audio_announcement = bap.BasicAudioAnnouncement(
            presentation_delay=40000,
            subgroups=[
                bap.BasicAudioAnnouncement.Subgroup(
                    codec_id=hci.CodingFormat(codec_id=hci.CodecID.LC3),
                    codec_specific_configuration=codec_config,
                    metadata=le_audio.Metadata(
                        [
                            le_audio.Metadata.Entry(
                                tag=le_audio.Metadata.Tag.LANGUAGE, data=b"eng"
                            ),
                            le_audio.Metadata.Entry(
                                tag=le_audio.Metadata.Tag.PROGRAM_INFO, data=b"Disco"
                            ),
                        ]
                    ),
                    bis=[
                        bap.BasicAudioAnnouncement.BIS(
                            index=1,
                            codec_specific_configuration=bap.CodecSpecificConfiguration(
                                audio_channel_allocation=bap.AudioLocation.FRONT_LEFT
                            ),
                        ),
                    ],
                )
            ],
        )

        advertising_set = await self.device.create_advertising_set(
            advertising_parameters=bumble.device.AdvertisingParameters(
                advertising_event_properties=bumble.device.AdvertisingEventProperties(
                    is_connectable=False
                ),
                primary_advertising_interval_min=100,
                primary_advertising_interval_max=200,
            ),
            advertising_data=(
                broadcast_audio_announcement.get_advertising_data()
                + bytes(
                    core.AdvertisingData(
                        [(core.AdvertisingData.BROADCAST_NAME, b"Bumble Auracast")]
                    )
                )
            ),
            periodic_advertising_parameters=bumble.device.PeriodicAdvertisingParameters(
                periodic_advertising_interval_min=80,
                periodic_advertising_interval_max=160,
            ),
            periodic_advertising_data=basic_audio_announcement.get_advertising_data(),
            auto_restart=True,
            auto_start=True,
        )
        logger.info("Start Periodic Advertising")
        await advertising_set.start_periodic()

        logger.info("Create BIG")
        big = await self.device.create_big(
            advertising_set,
            parameters=bumble.device.BigParameters(
                num_bis=1,
                sdu_interval=codec_config.frame_duration.us,
                max_sdu=codec_config.octets_per_codec_frame,
                max_transport_latency=65,
                rtn=3,
                broadcast_code=(
                    bytes.fromhex(broadcast_code) if broadcast_code else None
                ),
            ),
        )

        logger.info("Setup Controller Data Path")
        for bis_link in big.bis_links:
            await bis_link.setup_data_path(
                direction=bis_link.Direction.HOST_TO_CONTROLLER
            )

        self.bis_link = big.bis_links[0]
        self.device.host.on("packet_complete", self.on_iso_pdu_sent)
        self.bis_link.write(self.iso_packets[self.packet_sequence_number])
        self.packet_sequence_number += 1

        logger.info("Start sending frames ...")
        self.stream_started.set()

    async def wait_for_streaming(self, timeout: float):
        """
        Wait for the streaming to start.

        This method waits for the 'stream_started' event to be set, indicating that
        streaming has started. It logs a message when streaming starts successfully.

        Args:
            timeout (float): The maximum time to wait for the streaming to start.

        Raises:
            asyncio.TimeoutError: If the streaming does not start within the given timeout.
        """

        try:
            await asyncio.wait_for(self.stream_started.wait(), timeout)
            logging.info("Streaming started")
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError from error

    async def wait_for_complete(self, timeout: float):
        """
        Wait for the streaming to complete.

        This method waits for the 'send_complete' event to be set, indicating that
        all frames have been sent. It logs a message when streaming completes
        successfully.

        Args:
            timeout (float): The maximum time to wait for the streaming to complete.

        Raises:
            asyncio.TimeoutError: If the streaming does not complete within the given timeout.
        """

        try:
            await asyncio.wait_for(self.send_complete.wait(), timeout)
            logging.info("Streaming complete")
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError from error

    async def stop_streaming(self):
        """
        Stop the broadcasting stream and power off the device.

        This method signals the end of the broadcasting session by powering off
        the device. Ensure that all necessary operations are completed before
        invoking this method.
        """

        await self.device.power_off()