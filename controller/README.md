### Introduction ###
The following firmware binaries allow to use the nRF5340-DK as a HCI UART Controller 5.2+ with enabled 
Flow Control. 

### Programming ###
Before using the nRF53 programm nRF53DK_hci_uart.hex on the board (e.g. using nRF Connect for Desktop):


### UART Configuration ###
In order to use UART with Flow Control enabled the FLOW CONTROL switches for VCOM0 and VCOM1 must the set to OFF.

The UART runs with following configuration:

- BAUDRATE = 1000000 
- FLOWCONTROL = enabled
- DATA = 8 bits
- STOP = 1 bit
- PARITY = None

When the DK is connected to the PC via USB two VCOM Ports are created (e.g. COM5 and COM6).
Please use com port with the lower number.

