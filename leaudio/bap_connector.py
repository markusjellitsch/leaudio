"""
BAP Client Connector Class

Created on 26. Dec. 2024

@author: Markus Jellitsch
"""

# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
from bumble.profiles.ascs import (
    AudioStreamControlServiceProxy,
)
from bumble.profiles.pacs import (
    PublishedAudioCapabilitiesServiceProxy,
)
from bumble.profiles.bass import BroadcastAudioScanServiceProxy


from bumble.utils import AsyncRunner
from bumble.device import (
    Device,
    Peer,
    ConnectionParametersPreferences,
    Connection,
)
from bumble.core import AdvertisingData
from bumble.hci import HCI_LE_1M_PHY, HCI_LE_2M_PHY
import logging
import asyncio


class BapConnector(Device.Listener):
    dflt_target_name = "BUMBLE SINK"
    dflt_mtu_size = 1691

    def __init__(self, device):
        # additional device attributes
        self.device = device
        self.device.cis_enabled = True
        self.device.listener = self

        # connection related members
        self.target_name = self.dflt_target_name
        self.connected = asyncio.Event()

        # GATT client related members
        self.peer = None
        self.ascs_client = None
        self.pacs_client = None
        self.bass_client = None

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
        This method is called when a connection is established to the target device.

        It logs messages when the link is encrypted, the PHY is updated and the MTU is requested.
        It also creates clients for the Published Audio Capabilities Service, the
        Audio Stream Control Service and the Broadcast Audio Scan Service.

        Args:
            connection: The established connection.
        """
        self.peer = Peer(connection)
        logging.info(f"connection established => address: {connection.peer_address}")

        await connection.pair()
        logging.info("Link encrypted")

        await connection.set_phy(rx_phys=[HCI_LE_2M_PHY], tx_phys=[HCI_LE_2M_PHY])
        logging.info("PHY updated")

        await self.peer.request_mtu(self.dflt_mtu_size)
        logging.info("MTU requested")

        self.pacs_client = await self.peer.discover_service_and_create_proxy(
            PublishedAudioCapabilitiesServiceProxy
        )
        self.ascs_client = await self.peer.discover_service_and_create_proxy(
            AudioStreamControlServiceProxy
        )

        self.bass_client = await self.peer.discover_service_and_create_proxy(
            BroadcastAudioScanServiceProxy
        )

        self.connected.set()

    @property
    def is_connected(self):
        return self.peer is not None

    async def connect(self, target_name):
        """
        Start scanning for a device with the given target_name.

        Args:
            target_name: The name (complete local name) of the target device to connect to.

        """
        self.target_name = target_name
        await self.device.power_on()
        await self.device.start_scanning()

    async def wait_for_connection(self, timeout):
        """
        Wait for the connection to be established.

        This method waits for the connection to be established (i.e. the stream is started).
        It logs a message when the connection is established successfully.

        Args:
            timeout (float): The maximum time to wait for the connection to be established.

        Raises:
            asyncio.TimeoutError: If the connection is not established within the given timeout.
        """
        try:
            await asyncio.wait_for(self.connected.wait(), timeout)
            logging.info("Streaming started")
        except asyncio.TimeoutError as error:
            raise asyncio.TimeoutError from error

    async def disconnect(self):
        """
        Disconnect the connection to the target device.

        This method closes the connection to the target device if it is connected.
        It logs a message when the connection is closed successfully.

        Raises:
            asyncio.TimeoutError: If the connection is not closed within the given timeout.
        """
        if self.peer:
            await self.peer.connection.disconnect()
            self.peer = None
            self.ascs_client = None
            self.pacs_client = None
            self.bass_client = None
            logging.info("Connection closed")
