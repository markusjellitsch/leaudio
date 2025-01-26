"""
Application example using the BapBroadcastSource class
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
import logging
from typing import  Any, AsyncGenerator, Coroutine

import click
from leaudio import LeAudioEncoder
from bumble.colors import color
from bumble import core
from bumble import hci
from bumble.profiles import bap
from bumble.profiles import bass
import bumble.device
import bumble.transport
import bumble.utils

from leaudio import read_wav_file, generate_sine_data, BapBroadcastSource


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
AURACAST_DEFAULT_DEVICE_NAME = "Bumble Auracast"
AURACAST_DEFAULT_DEVICE_ADDRESS = hci.Address("F0:F1:F2:F3:F4:F5")


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
    broadcast_name: str,
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
            logging.info(f"Use Serial Port: {port}")
            logging.info(f"Use Broadcast ID: {broadcast_id}")
            logging.info(f"Use Broadcast Code: {broadcast_code}")
            logging.info(f"Use Broadcast Name: {broadcast_name}")
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
                broadcast_id, broadcast_code, broadcast_name,codec_config, repeat
            )
            await broacast_source.wait_for_streaming(10)
            if not repeat:
                await broacast_source.wait_for_complete(frame_num / 100 + 10)
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
    "--broadcast-name",
    metavar="BROADCAST_NAME",
    type=str,
    default="Bumble Broadcast",
    help="Broadcast name",
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

def main(
    port,
    broadcast_id,
    broadcast_code,
    broadcast_name,
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
            broadcast_name=broadcast_name,
            wav=wav,
            sample_rate=bap.SamplingFrequency(int(sample_rate)),
            sine_frequency=sine_frequency,
            duration_s=duration,
            repeat=not no_repeat,
            verbose=verbose,
        )
    )


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter


# ####### NOTES for the IOT747
# Set Baudrate to 9600
# SCAN 2 OFF 2
# open F0F1F2F3F4F5 BROAD 2 0 1e40
# MUSIC 91 PLAY
