
# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
from __future__ import annotations

import asyncio
import contextlib

import logging
from typing import Any, AsyncGenerator
import click
from bumble import hci
import bumble.device
import  bumble.transport
import bumble.utils

from leaudio.bap_broadcast_assistant import BapBroadcastAssistant



AURACAST_DEFAULT_DEVICE_NAME = 'Bumble Auracast'
AURACAST_DEFAULT_DEVICE_ADDRESS = hci.Address('F0:F1:F2:F3:F4:F5')

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

        yield device
    


async def run_assist(port,broadcast_id,broadcast_code,broadcast_address,target_name,verbose) -> None:

    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    async with create_device(port) as device:
        
        try:
            logging.info(f"Connecting to {port}")
            logging.info(f"Broadcast ID: {broadcast_id}")
            logging.info(f"Broadcast Code: {broadcast_code}")
            logging.info(f"Broadcast Address: {broadcast_address}")
            logging.info(f"Target Name: {target_name}")
            broadcast_assistant = BapBroadcastAssistant(device)
            await broadcast_assistant.connect(target_name)
            print("Waiting for connection")
            await broadcast_assistant.wait_for_connection(10)
            await broadcast_assistant.add_source(broadcast_address,broadcast_code,broadcast_id)
            await broadcast_assistant.peer.sustain(10)
            await broadcast_assistant.disconnect()
        except asyncio.TimeoutError as error:
            logging.error(f"Connection failed: {error}")
        finally:
            await broadcast_assistant.disconnect()
        

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
    default=123456,
    help='Broadcast ID',
)
@click.option(
    '--broadcast-code',
    metavar='BROADCAST_CODE',
    type=int,
    default=0,
    help='Braodcast Code',
)
@click.option(
    '--broadcast-address',
    metavar='BRAODCAST_ADDRESS',
    type=str,
    default="F0:F1:F2:F3:F4:F5",
    help='Broadcast Address (e.g.,"36:84:22:7f:b1:9d")',
)
@click.option(
    '--target-name',
    metavar='TARGET_NAME',
    type=str,
    default="BUMBLE",
    help='Scan Delegator target name'
)
@click.option(
    '--verbose',
    '-v',
    is_flag=True,
    default=False,
    help='Enable verbose logging',
)   
def main(port,broadcast_id,broadcast_code,broadcast_address,target_name,verbose):
    # 36:84:22:7f:b1:9d
   asyncio.run(run_assist(port,broadcast_id,broadcast_code,hci.Address(broadcast_address),target_name,verbose))

if __name__ == '__main__':
    main()