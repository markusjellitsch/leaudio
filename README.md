# LE Audio Client
This package shall provide BLE client examples (LE Audio, ASHA, etc) for streaming Audio using Google Bumble.

# Requirements
A HCI enabled BLE controller (e.g. nRF53 with hci_uart.hex) is required.

# Install
```

git clone https://github.com/markusjellitsch/leaudio.git
cd leaudio
pip install -e .
```

# Usage
run an client application for example the LE Audio Unicast Client:

```
unicast_client -p COMXY -w path/to/wav/file/your_wav.wav -t COMPLETE_LOCAL_NAME 
```

NOTE: You can use sound examples located in leaudio/sounds
