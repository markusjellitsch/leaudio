# le audio
This package shalle prove BLE client examples (LE Audio, ASHA, etc) for streaming Audio using Google Bumble.

# Requirements
A HCI enabled BLE controller (e.g. nRF53 with hci_uart.hex) is required.

# Install
''[git clone ](https://github.com/markusjellitsch/leaudio.git)
cd leaudio
pip install -e .
''

# Usage
run an client application for example:

'' 
unicast_client -p serial:COMXY -w path/to/wav/file/your_wav.wav -t COMPLETE_LOCAL_NAME 
''
