# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
AURACAST_DEFAULT_DEVICE_NAME = 'Bumble Auracast'
AURACAST_DEFAULT_DEVICE_ADDRESS = hci.Address('F0:F1:F2:F3:F4:F5')
AURACAST_DEFAULT_SYNC_TIMEOUT = 5.0
AURACAST_DEFAULT_ATT_MTU = 256

AURACAST_DEFAULT_SAMPLING_FREQUENCY = 48000
AURACAST_DEFAULT_FRAME_DURATION = 10000
iso_index: int = 0



@contextlib.asynccontextmanager
async def create_device(transport: str) -> AsyncGenerator[bumble.device.Device, Any]:
    transport = "serial:" +transport 
    async with await bumble.transport.open_transport(transport) as (
        hci_source,
        hci_sink,
    ):
        device_config = bumble.device.DeviceConfiguration(
            name=AURACAST_DEFAULT_DEVICE_NAME,
            address=AURACAST_DEFAULT_DEVICE_ADDRESS,
            keystore='JsonKeyStore',
        )
        
        
        device = bumble.device.Device.from_config_with_hci(
            device_config,
            hci_source,
            hci_sink,
        )
        await device.power_on()

        yield device


async def run_broadcast(
    transport: str, broadcast_id: int, broadcast_code: str | None, wav_file_path: str | None
) -> None:

    TEST_SINE = 1 if wav_file_path is None else 0
    print(f'Test Sine: {TEST_SINE}')
    encoder = LeAudioEncoder()
    async with create_device(transport) as device:
        if not device.supports_le_periodic_advertising:
            print(color('Periodic advertising not supported', 'red'))
            return

        # create snoop file
        f = open("log.btsnoop", "wb")
        Snooper = BtSnooper(f)
        device.host.snooper = Snooper
        
        # setup Lc3 encoder
        encoder.setup_encoders(48000, 10000, 1)

        frames = list[bytes]()
        
        if TEST_SINE == 1:    
            data_to_encode = generate_sine_data(1000, 48000, 0.01)
            num_runs = 2000
        else:
            sample_size = 480
            data_to_encode = read_wav_file(wav_file_path, 48000)
            num_runs = len(data_to_encode) // sample_size
        
        for i in range(num_runs):
            if TEST_SINE == 0:
                pcm = data_to_encode[i * sample_size:i*sample_size+sample_size]
                iso = encoder.encode(100, 1, 1, bytes(pcm))
            else:
                iso = encoder.encode(100, 1, 1, data_to_encode)

            frames.append(iso)

        del encoder
        print('Encoding complete.')

        basic_audio_announcement = bap.BasicAudioAnnouncement(
            presentation_delay=40000,
            subgroups=[
                bap.BasicAudioAnnouncement.Subgroup(
                    codec_id=hci.CodingFormat(codec_id=hci.CodecID.LC3),
                    codec_specific_configuration=bap.CodecSpecificConfiguration(
                        sampling_frequency=bap.SamplingFrequency.FREQ_48000,
                        frame_duration=bap.FrameDuration.DURATION_10000_US,
                        octets_per_codec_frame=100,
                    ),
                    metadata=le_audio.Metadata(
                        [
                            le_audio.Metadata.Entry(
                                tag=le_audio.Metadata.Tag.LANGUAGE, data=b'eng'
                            ),
                            le_audio.Metadata.Entry(
                                tag=le_audio.Metadata.Tag.PROGRAM_INFO, data=b'Disco'
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
        broadcast_audio_announcement = bap.BroadcastAudioAnnouncement(
            broadcast_id)
        print('Start Advertising')
        advertising_set = await device.create_advertising_set(
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
                        [(core.AdvertisingData.BROADCAST_NAME, b'Bumble Auracast')]
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
        print('Start Periodic Advertising')
        await advertising_set.start_periodic()
        print('Setup BIG')
        big = await device.create_big(
            advertising_set,
            parameters=bumble.device.BigParameters(
                num_bis=1,
                sdu_interval=10000,
                max_sdu=100,
                max_transport_latency=65,
                rtn=3,
                broadcast_code=(
                    bytes.fromhex(broadcast_code) if broadcast_code else None
                ),
            ),
        )
        print('Setup ISO Data Path')
        for bis_link in big.bis_links:
            await bis_link.setup_data_path(
                direction=bis_link.Direction.HOST_TO_CONTROLLER
            )

        dummy_frame = bytearray(100)
        big.bis_links[0].write(dummy_frame)
        # big.bis_links[1].write(dummy_frame)

        def on_iso_pdu_sent(event):

            global iso_index
            big.bis_links[0].write(frames[iso_index])
            iso_index += 1
            if iso_index == len(frames):
                iso_index = 0

        device.host.on('packet_complete', on_iso_pdu_sent)
        print('Start sending frames ...')
        while True:
            await asyncio.sleep(1)


def run_async(async_command: Coroutine) -> None:
    try:
        asyncio.run(async_command)
    except core.ProtocolError as error:
        if error.error_namespace == 'att' and error.error_code in list(
            bass.ApplicationError
        ):
            message = bass.ApplicationError(error.error_code).name
        else:
            message = str(error)

        print(
            color('!!! An error occurred while executing the command:', 'red'), message
        )


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

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
    '--broadcast_id',
    metavar='BROADCAST_ID',
    type=int,
    default=123456,
    help='Broadcast ID',
)
@click.option(
    '--broadcast-code',
    metavar='BROADCAST_CODE',
    type=str,
    help='Broadcast encryption code in hex format',
)
def broadcast(port, broadcast_id, broadcast_code, wav):
    """Start a broadcast as a source."""
    # ctx.ensure_object(dict)
    run_async(
        run_broadcast(
            transport=port,
            broadcast_id=broadcast_id,
            broadcast_code=broadcast_code,
            wav_file_path=wav,
        )
    )


def main():

    logging.basicConfig(level=os.environ.get(
        'BUMBLE_LOGLEVEL', 'ERROR').upper())
    broadcast()


# -----------------------------------------------------------------------------
if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter


# ####### NOTES for the IOT747
# Set Baudrate to 9600
# SCAN 2 OFF 2
# open F0F1F2F3F4F5 BROAD 2 0 1e40
# MUSIC 91 PLAY
