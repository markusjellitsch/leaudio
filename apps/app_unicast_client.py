"""
App using the BAP Unicast Client Class

Created on 26. Dec. 2024

@author: Markus Jellitsch
"""

from typing import Any, AsyncGenerator, Coroutine
import click
import contextlib
import serial as ser
from bumble.profiles.bap import (
    AudioLocation,
    SamplingFrequency,
    FrameDuration,
    CodecSpecificConfiguration,
)

from bumble.transport import open_transport
from bumble.device import (
    Device,
    DeviceConfiguration,
)
from bumble.snoop import BtSnooper
from bumble import core

import logging
from leaudio import LeAudioEncoder, BapUnicastClient
import asyncio
import time
from bumble import hci
import sys

from leaudio.utils import generate_sine_data, read_wav_file, get_octets_per_codec_frame


DFLT_DEVICE_NAME = "Bumble Unicast Client"
DFLT_DEVICE_ADDRESS = hci.Address("F0:F1:F2:F3:F4:F5")


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
            octets_per_codec_frame=get_octets_per_codec_frame(
                sample_rate, FrameDuration.DURATION_10000_US
            ),
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
            await bap_client.connect(target_name)
            await bap_client.wait_for_connection(12)
            await bap_client.start_streaming_until(codec_config,15)
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
