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
