# Example: three protocols at the same time

`firmware/examples/multi.asm` (212 words, default schedule) runs three unrelated protocols at once, on the four
threads, from one program image:

| Threads | Protocol | Pins |
|---|---|---|
| 0 and 1 | WS2812 LED strip driver: 8 pixels, rotating rainbow, 800 kHz (`ws2812.asm`) | `uo[2]` = DIN |
| 2 | I2C target at address 0x2A with 16 read/write registers, 100 and 400 kHz (`i2c_target_regs.asm`) | `uio[0]` = SDA, `uio[1]` = SCL (open drain, pull-ups on the board) |
| 3 | UART transmitter, 115200 baud 8N1: a status line `UP 00`, `UP 01`, ... about every 2 ms | `uo[0]` = TX |

```
python3 tools/load_image.py build/multi firmware/examples/multi.asm
cd test && make COCOTB_TEST_MODULES=test_multi
python3 tools/timing.py firmware/timing/multi_ws2812.timing firmware/timing/multi_uart.timing
```

## How the pins are shared

Nothing in the hardware assigns pins to threads: every thread can read every input and drive every output. The
programs divide the pins between them by convention. `PIN` and `ODRV` change one pin and leave the others alone,
so two threads can drive different pins of the same port without stepping on each other. Each thread also has its
own registers, its own return stack and its own 16 bytes of data memory (the I2C registers live in thread 2's).
The one shared resource is the one-byte mailbox, which only the WS2812 driver uses here (thread 1 feeds thread 0).

Because the threads take turns in a fixed order, a thread's timing depends only on its own instructions. That is
proved formally for the CPU (`docs/verification.md`, the isolation proofs). This is why the three programs could be
put together unchanged: the WS2812 and I2C code is the stand-alone examples' with the labels renamed, and the only
edit is that the I2C target no longer shows the last written byte on `uo[7:0]`, because those pins now belong to
the other protocols.

## What is checked

- `test/test_multi.py` (RTL, and the gate-level netlist in CI) runs all three at once through the pins: random I2C
  register writes and reads against a reference model at 400 kHz, and all 16 registers at 100 kHz, with the bus
  checks of `test_i2c_target_regs.py`. Meanwhile the WS2812 waveform is decoded (pixels and rotation) and every UART
  frame is checked against the 432-clock bit grid. The WS2812 pulse widths are exactly those of the stand-alone
  program, to the clock (320 / 720 ns high, 1.28 µs per bit), however busy the I2C bus is.
- `firmware/timing/multi_ws2812.timing` and `multi_uart.timing` prove the LED and UART bit timing again in this
  image: exactly 1.28 µs per LED bit and 8.64 µs per UART bit, on every path.

## Limits

- Switching to a different set of protocols means loading a new image: the loader holds the CPU in reset while it
  writes, so the protocols stop for the few milliseconds that takes. Protocols cannot be swapped one thread at a time
  while the others keep running.
- With all four threads busy, each runs one instruction every 80 ns. A protocol that needs the faster schedule (the
  USB and CAN receivers use two of the four turns) cannot be combined with three others.
