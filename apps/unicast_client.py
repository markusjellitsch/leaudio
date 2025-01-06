from scipy import signal
import numpy as np
import argparse
from bumble.profiles.bap import (

    AudioLocation,

    SamplingFrequency,

    FrameDuration,

    CodecSpecificConfiguration,

)
from bumble.profiles.ascs import (

    AudioStreamControlServiceProxy,

    ASE_Config_Codec,

    ASE_Config_QOS,

    ASE_Disable,

    ASE_Enable,

)
from bumble.hci import CodecID, CodingFormat
from bumble.profiles.pacs import (

    PacRecord,

    PublishedAudioCapabilitiesServiceProxy,

)
from bumble.transport.common import StreamPacketSource
from bumble.profiles import pacs
from bumble.utils import AsyncRunner
from bumble.transport import open_transport_or_link
from bumble.device import Device, Peer, Advertisement, ConnectionParametersPreferences, Connection
from bumble.core import ProtocolError, AdvertisingData
from bumble.snoop import BtSnooper
import functools
from bumble.colors import color
from bumble.profiles.ascs import AudioStreamControlServiceProxy
from bumble.hci import HCI_IsoDataPacket, HCI_LE_1M_PHY, HCI_LE_2M_PHY
from bumble import device
from typing import Optional, List, cast
import scipy.io.wavfile as wav
import logging
import sys
sys.path.append('../utils') 
from le_audio_encoder import LeAudioEncoder
import asyncio
import sys
import time
import os
from bumble import hci
import sys


app_specific_codec = CodecSpecificConfiguration( 
    sampling_frequency=SamplingFrequency.FREQ_24000,
    frame_duration=FrameDuration.DURATION_10000_US,
    audio_channel_allocation=AudioLocation.FRONT_RIGHT,    
    octets_per_codec_frame=60,
    codec_frames_per_sdu=1,
)


TEST_SINE = 1


complete_local_name = "BLE_COVER"
iso_packets = []

upsampled_left_channel = None


def read_wav_file(filename):

    rate, data = wav.read(filename)

    print("Bitdepth:", data.dtype.itemsize * 8)

    print("Sample rate:", rate)

    left_channel = data[:, 1]

    print("Audio data (left):", left_channel)

    print(len(left_channel))
    print(app_specific_codec.sampling_frequency.hz)
    upsampled_data = signal.resample(left_channel, int(
        app_specific_codec.sampling_frequency.hz / 41000 * left_channel.shape[0]))

    wav.write("upsampled_stereo_file.wav", app_specific_codec.sampling_frequency.hz, upsampled_data.astype(data.dtype))

    return upsampled_data.astype(np.int16)


def generate_sine_wave_iso_frames(frequency, sampling_rate, duration):
    num_samples = int(sampling_rate * duration)

    t = np.linspace(0, duration, num_samples, False)

    sine_wave = np.sin(2 * np.pi * frequency * t)

    # Scale the sine wave to the 16-bit range (-32768 to 32767)

    scaled_sine_wave = sine_wave * 8191.5

    # Convert to 16-bit integer format
    int16_sine_wave = scaled_sine_wave.astype(np.int16)

    iso_frame = bytearray()

    for num in int16_sine_wave:

        iso_frame.append(num & 0xFF)  # Extract lower 8 bits

        iso_frame.append((num >> 8) & 0xFF)  # Extract upper 8 bit

    return iso_frame


# -----------------------------------------------------------------------------

class Listener(Device.Listener):

    packet_sequence_number = 0

    def __init__(self, device):

        self.device = device

    @AsyncRunner.run_in_task()
    async def on_advertisement(self, advertisement):

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

                    params = ConnectionParametersPreferences()

                    params.connection_interval_min = (47)

                    params.connection_interval_max = (47)

                    prefs = {HCI_LE_1M_PHY: params, HCI_LE_2M_PHY: params}

                    await self.device.connect(peer_address=advertisement.address, connection_parameters_preferences=prefs)

    @AsyncRunner.run_in_task()
    async def on_connection(self, connection: Connection):

        notifications = {1: asyncio.Queue()}

        def on_notification(data: bytes, ase_id: int):

            notifications[ase_id].put_nowait(data)

        print(f'=== Connected to {connection}')

        peer = Peer(connection)

        # set PHY to 2M
        await connection.set_phy(rx_phys=[HCI_LE_2M_PHY], tx_phys=[HCI_LE_2M_PHY])

        # pair with the device
        await connection.pair()

        # request mtu change
        mtu = await peer.request_mtu(1691)

        # get remote features
        remote_features = await self.device.get_remote_le_features(connection)

        print(f"peer supports the following features:{remote_features}")

        # discover services
        pacs_client = await peer.discover_service_and_create_proxy(PublishedAudioCapabilitiesServiceProxy)
        ascs_client = await peer.discover_service_and_create_proxy(AudioStreamControlServiceProxy)
        
        # read sink PACs
        response = await pacs_client.sink_pac.read_value()
        pac_record = PacRecord.from_bytes(response[1:])
        print(pac_record)
        
        # enable ASCS notifications
        await ascs_client.ase_control_point.subscribe()
        await ascs_client.sink_ase[0].subscribe(
            functools.partial(on_notification, ase_id=1)
        )

        # read sink ASE state
        sink_state = await ascs_client.sink_ase[0].read_value()
        print(sink_state)

        print(app_specific_codec)
        await ascs_client.ase_control_point.write_value(
            ASE_Config_Codec(
                ase_id=[1],
                target_latency=[0x3],
                target_phy=[2],
                codec_id=[CodingFormat(CodecID.LC3)],
                codec_specific_configuration=[app_specific_codec],
            )
            
            
        )
        
        # wait for notification
        await notifications[1].get()
        print("ASE: codec configured")

        # setup the CIG
        cis_handles = await self.device.setup_cig(
            cig_id=1,
            cis_id=[1],
            sdu_interval=(app_specific_codec.frame_duration.us, app_specific_codec.frame_duration.us),
            framing=0,
            max_sdu=(app_specific_codec.octets_per_codec_frame, 0),
            retransmission_number=15,
            max_transport_latency=(95, 95),
        )

        # configure  ASE (config QOS)
        await ascs_client.ase_control_point.write_value(

            ASE_Config_QOS(
                ase_id=[1],
                cig_id=[1],
                cis_id=[1],
                sdu_interval=[app_specific_codec.frame_duration.us],
                framing=[0],
                phy=[2],
                max_sdu=[app_specific_codec.octets_per_codec_frame],
                retransmission_number=[15],
                max_transport_latency=[95],
                presentation_delay=[40000],
            )
        )

        # wait for notifications
        await notifications[1].get()
        print("ASE: QOS configured")

        # configure ASE (Enable)
        await ascs_client.ase_control_point.write_value(
            ASE_Enable(
                ase_id=[1],
                metadata=[bytes([0x03, 0x02, 0x01, 0x00])],
            )
        )

        # wait for notifications
        await notifications[1].get()
        print('ASE: enabling')

        # create CIS
        await self.device.create_cis(
            [
                (cis_handles[0], connection.handle)
            ]
        )

        print('ASE: cis established')

        await self.device.send_command(
            hci.HCI_LE_Setup_ISO_Data_Path_Command(
                connection_handle=cis_handles[0],
                data_path_direction=hci.HCI_LE_Setup_ISO_Data_Path_Command.Direction.HOST_TO_CONTROLLER,
                data_path_id=0x00,  # Fixed HCI
                codec_id=hci.CodingFormat(hci.CodecID.TRANSPARENT),
                controller_delay=0,
                codec_configuration=b'',
            ))

        # wait for notifications
        await notifications[1].get()
        print('ASE: audio stream enabled')

        # prepere the ISO packets
        self.packet_sequence_number = 0 
        self.iso_packet = HCI_IsoDataPacket(
            connection_handle=cis_handles[0],
            data_total_length=app_specific_codec.octets_per_codec_frame + 4,
            packet_sequence_number=self.packet_sequence_number,
            pb_flag=0b10,
            packet_status_flag=0,
            iso_sdu_length=app_specific_codec.octets_per_codec_frame,
            iso_sdu_fragment=bytes([0]*app_specific_codec.octets_per_codec_frame),
        )

        self.send_complete = False
        def on_iso_pdu_sent(event):
            if self.packet_sequence_number < len(iso_packets) - 1: 
                # send the next ISO packet
                self.packet_sequence_number += 1
                self.iso_packet.packet_sequence_number = self.packet_sequence_number
                self.iso_packet.iso_sdu_fragment = iso_packets[self.packet_sequence_number]
                self.device.host.send_hci_packet(self.iso_packet)
            else:
                self.send_complete = True

        self.device.host.on('iso_packet_sent', on_iso_pdu_sent)
        self.device.host.send_hci_packet(self.iso_packet)

        while True:
            await asyncio.sleep(1)
            if self.send_complete:
                print("send complete. bye bye")
                await self.device.power_off()
                self.device.future.set_result(None)
                break

# -----------------------------------------------------------------------------


async def main() -> None:
    global complete_local_name
    parser = argparse.ArgumentParser(
        description="A simple example of argparse")
    parser.add_argument("-c", "--config", type=str,
                        default="device.json", help="device config file")
    parser.add_argument(
        "-p", "--port", help="com port (e.g. serial:/dev/ttyUSB0)")
    parser.add_argument("-s", "--sample_rate", type=int,
                        default=0, help="choose a sample rate")
    parser.add_argument("-f", "--frame_duration", type=int,
                        default=0, help="choose a frame duration")
    parser.add_argument("-w", "--wave", type=str,
                        help="choose a frame duration")
    parser.add_argument("-t", "--target_name", type=str,
                        help="target complete local name of the peer")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    args = parser.parse_args()


    if args.verbose > 0:
        logging.basicConfig(level=logging.DEBUG)
    else:

        logging.basicConfig(level=logging.ERROR)

    print("sample rate", args.sample_rate)
    if not args.sample_rate or args.sample_rate == 0:
        app_specific_codec.sampling_frequency = SamplingFrequency.FREQ_16000
        app_specific_codec.frame_duration = FrameDuration.DURATION_10000_US
        app_specific_codec.octets_per_codec_frame = 40
    elif args.sample_rate == 1:
        app_specific_codec.sampling_frequency = SamplingFrequency.FREQ_24000
        app_specific_codec.frame_duration = FrameDuration.DURATION_10000_US
        app_specific_codec.octets_per_codec_frame = 60
    elif args.sample_rate == 2:
        app_specific_codec.sampling_frequency = SamplingFrequency.FREQ_48000
        app_specific_codec.frame_duration = FrameDuration.DURATION_10000_US
        app_specific_codec.octets_per_codec_frame = 120
    else:
        raise ValueError("unknown sample rate")

    print(f"sample rate: {app_specific_codec.sampling_frequency.hz} Hz")
    print(f"frame duration: {app_specific_codec.frame_duration.us}  us")
    print(f"octets per codec frame: {app_specific_codec.octets_per_codec_frame} bytes")  

    async with await open_transport_or_link(args.port) as hci_transport:

        # Create a device to manage the host, with a custom listener
        device = Device.from_config_file_with_hci(
            args.config, hci_transport.source, hci_transport.sink
        )

        # Connect to
        if args.target_name:
            complete_local_name = args.target_name

        device.listener = Listener(device)
        device.cis_enabled = True

        f = open("log.btsnoop", "wb")

        Snooper = BtSnooper(f)

        device.host.snooper = Snooper

        if args.wave:
            sound_file = args.wave
            TEST_SINE = 0
        else:
            TEST_SINE = 1

        await device.power_on()
        encoder = LeAudioEncoder()
        encoder.setup_encoders(
            app_specific_codec.sampling_frequency.hz,
            app_specific_codec.frame_duration.us,
            1,
        )

        # prepare the samples
        num_runs = 0
        # calculate the number of samples per frame duration.
        sample_size = int(app_specific_codec.sampling_frequency.hz  * app_specific_codec.frame_duration.us / 1000 / 1000)
        if TEST_SINE == 0:

            if os.path.isfile(sound_file):
                upsampled_left_channel = read_wav_file(sound_file)
            else:
                raise FileNotFoundError(f"The file {sound_file} does not exist.")

            num_runs = len(upsampled_left_channel) // sample_size

        else:

            num_runs = 2000


        print("sample size", sample_size)
        for i in range(num_runs):

            if TEST_SINE == 0:
              
                pcm_data = upsampled_left_channel[i *sample_size:i*sample_size+sample_size]

            else:

                pcm_data = generate_sine_wave_iso_frames(
                    1000, app_specific_codec.sampling_frequency.hz, app_specific_codec.frame_duration.us / 1000000)

            data = encoder.encode(app_specific_codec.octets_per_codec_frame, 1, 1, bytes(pcm_data))
            iso_packets.append(data)

        print("finished with encoding", len(iso_packets))

        print(f'start scanning for  {complete_local_name}...')
        await device.start_scanning(scanning_phys=[HCI_LE_1M_PHY], legacy=False)
        
        device.future = asyncio.get_running_loop().create_future()
        await device.future 
        print("done")


# -----------------------------------------------------------------------------


asyncio.run(main())
