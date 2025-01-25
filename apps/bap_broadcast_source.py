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
from bumble.snoop import BtSnooper
import asyncio
import contextlib
import dataclasses
import functools
import logging
import os
from typing import cast, Any, AsyncGenerator, Coroutine, Dict, Optional, Tuple
from scipy import signal
import scipy.io.wavfile as wav
import click
import numpy as np
import pyee

import sys
from leaudio import LeAudioEncoder

from bumble.colors import color
from bumble import company_ids
from bumble import core
from bumble import gatt
from bumble import hci
from bumble.profiles import bap
from bumble.profiles import le_audio
from bumble.profiles import pbp
from bumble.profiles import bass
import bumble.device
import bumble.transport
import bumble.utils

from leaudio import read_wav_file, generate_sine_data


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
AURACAST_DEFAULT_DEVICE_NAME = "Bumble Auracast"
AURACAST_DEFAULT_DEVICE_ADDRESS = hci.Address("F0:F1:F2:F3:F4:F5")


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


@contextlib.asynccontextmanager
async def create_device(transport: str) -> AsyncGenerator[bumble.device.Device, Any]:
    transport = "serial:" + transport
    async with await bumble.transport.open_transport(transport) as (
        hci_source,
        hci_sink,
    ):
        device_config = bumble.device.DeviceConfiguration(
            name=AURACAST_DEFAULT_DEVICE_NAME,
            address=AURACAST_DEFAULT_DEVICE_ADDRESS,
            keystore="JsonKeyStore",
        )

        device = bumble.device.Device.from_config_with_hci(
            device_config,
            hci_source,
            hci_sink,
        )

        yield device


async def run_broadcast(
    port: str,
    broadcast_id: int,
    broadcast_code: str | None,
    wav: str | None,
    sample_rate: int,
    sine_frequency: int,
    duration_s: int,
    repeat: bool,
    verbose: int,
) -> None:
    if verbose > 0:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    async with create_device(port) as device:
        # create snoop file
        f = open("log.btsnoop", "wb")
        Snooper = BtSnooper(f)
        device.host.snooper = Snooper

        codec_config = bap.CodecSpecificConfiguration(
            sampling_frequency=sample_rate,
            frame_duration=bap.FrameDuration.DURATION_10000_US,
            audio_channel_allocation=bap.AudioLocation.FRONT_RIGHT,
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
            print("Starting broadcast source")
            logging.info(f"Use Serial Port: {port}")
            logging.info(f"Use Frame Duration: {codec_config.frame_duration.us} us")
            logging.info(
                f"Use Octets per Codec Frame: {codec_config.octets_per_codec_frame} bytes"
            )
            logging.info(
                f"Use Codec Frames per SDU: {codec_config.codec_frames_per_sdu}"
            )

            logging.info(f"Use Repeat Mode: {repeat}")
            if wav is not None:
                logging.info(f"Use Wav File: {wav}")
                logging.info(f"Use Duration: {frame_num / 100} s")
            else:
                logging.info(f"Use Sine Frequency: {sine_frequency} Hz")
                logging.info(f"Use Duration: {frame_num / 100} s")

            broacast_source = BapBroadcastSource(device=device)
            broacast_source.set_iso_data(iso_packets)
            await broacast_source.start_streaming(
                broadcast_id, broadcast_code, codec_config, repeat
            )
            await broacast_source.wait_for_streaming(10)
            if not repeat:
                await broacast_source.wait_for_complete(frame_num/100 + 10)
                logging.info("Streaming complete")
            else:
                logger.info("Streaming in repeat mode.Manual stop required")
                while True:
                    await asyncio.sleep(1)

        except asyncio.TimeoutError:
            logging.error("Timeout waiting for streaming to start/complete")
        finally:
            await broacast_source.stop_streaming()
            logging.info("Bye!!!")


def run_async(async_command: Coroutine) -> None:
    try:
        asyncio.run(async_command)
    except core.ProtocolError as error:
        if error.error_namespace == "att" and error.error_code in list(
            bass.ApplicationError
        ):
            message = bass.ApplicationError(error.error_code).name
        else:
            message = str(error)

        print(
            color("!!! An error occurred while executing the command:", "red"), message
        )


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


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
    "--broadcast_id",
    metavar="BROADCAST_ID",
    type=int,
    default=123456,
    help="Broadcast ID",
)
@click.option(
    "--broadcast-code",
    metavar="BROADCAST_CODE",
    type=str,
    help="Broadcast encryption code in hex format",
)
@click.option(
    "--sample-rate",
    metavar="SAMPLE_RATE",
    type=click.Choice(
        [
            str(bap.SamplingFrequency.FREQ_16000.value),
            str(bap.SamplingFrequency.FREQ_24000.value),
            str(bap.SamplingFrequency.FREQ_48000.value),
        ]
    ),
    default=str(bap.SamplingFrequency.FREQ_24000.value),
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
    "--no-repeat",
    "-r",
    is_flag=True,
    default=False,
    help="Not repeating the audio stream",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="Enable verbose logging",
)
def broadcast(
    port,
    broadcast_id,
    broadcast_code,
    wav,
    sample_rate,
    sine_frequency,
    duration,
    no_repeat,
    verbose,
):
    """Start a broadcast as a source."""
    # ctx.ensure_object(dict)
    run_async(
        run_broadcast(
            port=port,
            broadcast_id=broadcast_id,
            broadcast_code=broadcast_code,
            wav=wav,
            sample_rate=bap.SamplingFrequency(int(sample_rate)),
            sine_frequency=sine_frequency,
            duration_s=duration,
            repeat=not no_repeat,
            verbose=verbose,
        )
    )


def main():
    broadcast()


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter


# ####### NOTES for the IOT747
# Set Baudrate to 9600
# SCAN 2 OFF 2
# open F0F1F2F3F4F5 BROAD 2 0 1e40
# MUSIC 91 PLAY
