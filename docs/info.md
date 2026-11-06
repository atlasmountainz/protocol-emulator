<!---

This file is used to generate your project datasheet.

-->

## How it works

This project is an entry in Jane Street's protocol emulator ASIC competition. The goal is a small,
reprogrammable CPU that can bit-bang serial protocols (UART, SPI, I2C and more) from firmware.

The current milestone is a fixed-function 8N1 UART transmitter (`src/uart_tx.v`). The line idles high,
and each frame is a start bit (0), eight data bits LSB first, and a stop bit (1). The baud rate is set
by the `CLKS_PER_BIT` parameter (434 = 50 MHz clock at 115200 baud).

The next milestone replaces this fixed logic with a programmable core that runs the same UART as a program.

## How to test

1. Put a byte on `ui[7:0]`.
2. Raise `uio[0]` (START) for at least one clock. Only the rising edge matters, so holding it high sends one byte.
3. Watch `uo[0]` (TX) for the frame. `uo[1]` (BUSY) is high until the stop bit finishes.

Simulation tests live in `test/` and run with `make` using cocotb and Icarus Verilog.

## External hardware

A USB-to-serial adapter (or logic analyzer) on `uo[0]` to receive the UART output.
