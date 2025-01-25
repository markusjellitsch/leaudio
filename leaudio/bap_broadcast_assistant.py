# -----------------------------------------------------------------------------
# Imports
# -----------------------------------------------------------------------------
import logging
from bumble import core
from bumble.utils import AsyncRunner
from leaudio.bap_connector import BapConnector
from bumble import hci
from bumble.profiles import bass

logging.basicConfig(level=logging.INFO)
class BapBroadcastAssistant(BapConnector):

    dflt_address =hci.Address("F0:F1:F2:F3:F4:F5")
    def __init__(self, device):
        super().__init__(device)
        self.broadcast_id = None
        self.broadcast_code = 0
        self.broadcast_address = self.dflt_address

    
    async def add_source(self, broadcast_address, broadcast_code, broadcast_id):

        self.broadcast_address = broadcast_address
        self.broadcast_code = broadcast_code
        self.broadcast_id = broadcast_id
    
        # Check that the service was found
        if not self.bass_client:
            logging.error("BAP Broadcast Assistant: BAP Service not found")
            return

        #Subscribe to and read the broadcast receive state characteristics
        for i, broadcast_receive_state in enumerate(
            self.bass_client.broadcast_receive_states
        ):
            try:
                await broadcast_receive_state.subscribe(
                    lambda value, i=i: logging.info(
                        f"(f'Broadcast Receive State Update [{i}]:', 'green') {value}"
                    )
                )
            except core.ProtocolError as error:
                logging.error(f"Failed to subscribe: {error}")


        # print(color('Adding source:', 'blue'), broadcast.sync.advertiser_address)
        await self.bass_client.add_source(
            self.broadcast_address,
            self.broadcast_code,
            self.broadcast_id,
            bass.PeriodicAdvertisingSyncParams.SYNCHRONIZE_TO_PA_PAST_NOT_AVAILABLE,
            0xFFFF,
            [
                bass.SubgroupInfo(
                    bass.SubgroupInfo.ANY_BIS,
                    bytes(0),
                )
            ],
        )
        logging.info("Wrote source to scan delegator")



