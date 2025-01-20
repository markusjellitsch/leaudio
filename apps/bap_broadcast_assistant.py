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

import asyncio
import contextlib
import dataclasses
import functools

import logging
import os
import wave
import itertools
from typing import cast, Any, AsyncGenerator, Coroutine, Dict, Optional, Tuple
from bumble.hci import HCI_LE_1M_PHY, HCI_LE_2M_PHY
from bumble.core import AdvertisingData
import click
import pyee
from bumble.utils import AsyncRunner
from bumble.colors import color
from bumble import company_ids
from bumble import core
from bumble import gatt
from bumble import hci
from bumble.profiles import bap
from bumble.profiles import le_audio
from bumble.profiles import pbp
from bumble.profiles import bass
from bumble.device import Device
import bumble.device
import  bumble.transport
import bumble.utils

AURACAST_DEFAULT_DEVICE_NAME = 'Bumble Auracast'
AURACAST_DEFAULT_DEVICE_ADDRESS = hci.Address('F0:F1:F2:F3:F4:F5')
AURACAST_DEFAULT_ATT_MTU = 256

complete_local_name = "BUMBLE"
target_broadcast_id = 123456



@contextlib.asynccontextmanager
async def create_device(transport: str) -> AsyncGenerator[bumble.device.Device, Any]:


    transport = "serial:"+transport
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

class Listener(Device.Listener):
    global complete_local_name
    global target_broadcast_id

    def __init__(self, device):
        self.device = device

    @AsyncRunner.run_in_task()
    async def on_advertisement(self,advertisement):
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
                    await self.device.connect(peer_address=advertisement.address)
    
    @AsyncRunner.run_in_task()
    async def on_connection(self, connection):

        # Connect to the server
        peer = bumble.device.Peer(connection)
        print(f'=== Connected to {peer}')

        print("+++ Encrypting connection...")
        await peer.connection.pair()
        print("+++ Connection encrypted")

        # Request a larger MTU
        mtu = AURACAST_DEFAULT_ATT_MTU
        print(color(f'$$$ Requesting MTU={mtu}', 'yellow'))
        await peer.request_mtu(mtu)

        # Get the BASS service
        bass_client = await peer.discover_service_and_create_proxy(
            bass.BroadcastAudioScanServiceProxy
        )

        # Check that the service was found
        if not bass_client:
            print(color('!!! Broadcast Audio Scan Service not found', 'red'))
            return

        #Subscribe to and read the broadcast receive state characteristics
        for i, broadcast_receive_state in enumerate(
            bass_client.broadcast_receive_states
        ):
            try:
                await broadcast_receive_state.subscribe(
                    lambda value, i=i: print(
                        f"{color(f'Broadcast Receive State Update [{i}]:', 'green')} {value}"
                    )
                )
            except core.ProtocolError as error:
                print(f"Failed to subscribe: {error}")


        # print(color('Adding source:', 'blue'), broadcast.sync.advertiser_address)
        await bass_client.add_source(
            hci.Address("F0:F1:F2:F3:F4:F5"),
            0,
            target_broadcast_id,
            bass.PeriodicAdvertisingSyncParams.SYNCHRONIZE_TO_PA_PAST_NOT_AVAILABLE,
            0xFFFF,
            [
                bass.SubgroupInfo(
                    bass.SubgroupInfo.ANY_BIS,
                    bytes(0),
                )
            ],
        )

        # Notify the sink that we're done scanning.
        #await bass_client.remote_scan_stopped()
        
        await peer.sustain()
        return

async def run_assist(port,broadcast_id, target_name,verbose) -> None:
    global complete_local_name
    global target_broadcast_id
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.ERROR)
    async with create_device(port) as device:


        if not device.supports_le_periodic_advertising:
            print(color('Periodic advertising not supported', 'red'))
            return
    
        complete_local_name = target_name
        target_broadcast_id = broadcast_id
        device.listener = Listener(device)
        print('start scanning ...',complete_local_name)
        await device.start_scanning(scanning_phys=[HCI_LE_1M_PHY], legacy=False)
        await asyncio.get_running_loop().create_future()
        

@click.command()
@click.argument(
    'port',
    metavar='com port',
    type=str,
)
@click.option(
    '--broadcast-id',
    metavar='Broadcast ID',
    type=int,
    help='Braodcast ID',
)
@click.option(
    '--target-name',
    metavar='TARGET_NAME',
    type=str,
    default="BUMBLE",
    help='Target name'
)

@click.option(
    '--verbose',
    '-v',
    is_flag=True,
    default=False,
    help='Enable verbose logging',
)
def assistant(port,broadcast_id, target_name,verbose):
    asyncio.run(run_assist(port,broadcast_id, target_name,verbose))


def main():
    # 36:84:22:7f:b1:9d
    assistant()

if __name__ == '__main__':
    main()