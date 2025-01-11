
import wasmtime
import ctypes
from typing import List, cast
import wasmtime.loader
import leaudio.liblc3 as liblc3 # type: ignore
import enum

store = wasmtime.loader.store

_memory = cast(wasmtime.Memory, liblc3.memory)

STACK_POINTER = _memory.data_len(store)

_memory.grow(store, 1)

# Mapping wasmtime memory to linear address

memory = (ctypes.c_ubyte * _memory.data_len(store)).from_address(

    ctypes.addressof(_memory.data_ptr(store).contents)  # type: ignore

)


class Liblc3PcmFormat(enum.IntEnum):

    S16 = 0

    S24 = 1

    S24_3LE = 2

    FLOAT = 3


DEFAULT_PCM_SAMPLE_RATE = 48000
MAX_DECODER_SIZE = liblc3.lc3_decoder_size(10000, DEFAULT_PCM_SAMPLE_RATE)
MAX_ENCODER_SIZE = liblc3.lc3_encoder_size(10000, DEFAULT_PCM_SAMPLE_RATE)

DECODER_STACK_POINTER = STACK_POINTER
ENCODER_STACK_POINTER = DECODER_STACK_POINTER + MAX_DECODER_SIZE * 2
DECODE_BUFFER_STACK_POINTER = ENCODER_STACK_POINTER + MAX_ENCODER_SIZE * 2
ENCODE_BUFFER_STACK_POINTER = DECODE_BUFFER_STACK_POINTER + 8192


DEFAULT_PCM_FORMAT = Liblc3PcmFormat

DEFAULT_PCM_BYTES_PER_SAMPLE = 2


class LeAudioEncoder:

    def __init__(self):
        self.encoders: List[int] = []
        pass

    def setup_encoders(self, sample_rate: int, frame_duration_us: int, num_channels: int) -> None:
        """Setup LE audio encoders

        Args:
            sample_rate (int): Sample rate in Hz
            frame_duration_us (int): Frame duration in microseconds
            num_channels (int): Number of channels
        """
        self.encoders[:num_channels] = [
            liblc3.lc3_setup_encoder(
                frame_duration_us,
                sample_rate,
                0,  # Input sample rate
                ENCODER_STACK_POINTER + MAX_ENCODER_SIZE * i,
            )
            for i in range(num_channels)
        ]

    def encode(
        self,
        sdu_length: int,
        num_channels: int,
        input_stride: int,
        input_data: bytes,
    ) -> bytes:
        """Encode a LE audio frame

        Args:
            sdu_length (int): Length of the SDU
            num_channels (int): Number of channels
            input_stride (int): Stride of the input data
            input_data (bytes): Input data to encode

        Returns:
            bytes: Encoded data
        """
        if not input_data:
            return b""

        input_buffer_offset = ENCODE_BUFFER_STACK_POINTER
        input_buffer_size = len(input_data)

        # Copy into wasm memory
        memory[input_buffer_offset : input_buffer_offset + input_buffer_size] = input_data

        output_buffer_offset = input_buffer_offset + input_buffer_size
        output_buffer_size = sdu_length
        output_frame_size = output_buffer_size // num_channels

        for i in range(num_channels):
            result = liblc3.lc3_encode(
                self.encoders[i],
                0,
                input_buffer_offset + DEFAULT_PCM_BYTES_PER_SAMPLE * i,
                input_stride,
                output_frame_size,
                output_buffer_offset + output_frame_size * i,
            )

            if result != 0:
                raise RuntimeError(f"lc3_encode failed, result={result}")

        # Extract encoded data from the output buffer
        return bytes(memory[output_buffer_offset : output_buffer_offset + output_buffer_size])
