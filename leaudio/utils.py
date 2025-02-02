from scipy import signal
import scipy.io.wavfile as wav
import numpy as np
from bumble.profiles.bap import (
    SamplingFrequency,
    FrameDuration,
    CodecSpecificConfiguration
)
from leaudio import LeAudioEncoder

def read_wav_file(filename,target_sample_rate):

    rate, data = wav.read(filename)
    num_channels = data.ndim
        
    if num_channels == 1:
        left_channel = data[:]
    else: 
        left_channel = data[:, 1]
   
    print(len(left_channel))
    upsampled_data = signal.resample(left_channel, int(
        target_sample_rate / rate * left_channel.shape[0]))

    # wav.write("upsampled_stereo_file.wav", app_specific_codec.sampling_frequency.hz, upsampled_data.astype(data.dtype))
    print("Sample rate:", rate)
    print("Number channels:", num_channels)
    print("Audio data (left):", left_channel)
    print("Bitdepth:", data.dtype.itemsize * 8)

    return upsampled_data.astype(np.int16)


def generate_sine_data(frequency, sampling_rate, duration):


    t = np.arange(0, duration, 1/sampling_rate)

    sine_wave = np.sin(2 * np.pi * frequency * t)

    # Scale the sine wave to the 16-bit range (-32768 to 32767)
    scaled_sine_wave = sine_wave * 8191.5

    # Convert to 16-bit integer format
    return scaled_sine_wave.astype(np.int16)

def generate_iso_data(raw_data, codec_config:CodecSpecificConfiguration):


    frame_size = int(
        codec_config.sampling_frequency.hz
        * codec_config.frame_duration.us
        / 1000
        / 1000
    )

        
    frame_num = int(len(raw_data) // frame_size)
            
    encoder = LeAudioEncoder()
    encoder.setup_encoders(
        codec_config.sampling_frequency.hz,
        codec_config.frame_duration.us,
        1,
    )

    iso_packets = []
    for i in range(frame_num):
        pcm_data = raw_data[i * frame_size : i * frame_size + frame_size]
        data = encoder.encode(
            codec_config.octets_per_codec_frame, 1, 1, bytes(pcm_data)
        )
        iso_packets.append(data)
    return iso_packets


def get_octets_per_codec_frame(sampling_frequency:SamplingFrequency, frame_duration:FrameDuration):
    
    if frame_duration == FrameDuration.DURATION_10000_US:
        if sampling_frequency == SamplingFrequency.FREQ_16000:
            return 40
        elif sampling_frequency == SamplingFrequency.FREQ_24000:
            return 60
        elif sampling_frequency == SamplingFrequency.FREQ_48000:
            return 120
        else:
            raise ValueError("Invalid sampling frequency")
    else:
        raise ValueError("Invalid frame duration")
    