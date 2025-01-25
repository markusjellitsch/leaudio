from scipy import signal
import scipy.io.wavfile as wav
import numpy as np

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
