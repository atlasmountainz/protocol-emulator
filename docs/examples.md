# Loadable example programs

The chip's ROM holds four built-in protocols (UART transmitter, SPI master, I2C master with an SCL monitor,
full-duplex UART echo). Everything below is firmware that the host loads over the pins into the 256-word
program memory (`docs/loading.md`); none of it is in the ROM and none of it needed a hardware change. Each
program has a cocotb test module that loads it through the real pin loader and drives it with a protocol
model that checks the timing windows (`docs/verification.md`).

| Program | Role | Words | Document | Tests |
|---|---|---|---|---|
| `firmware/examples/i2c_target.asm` | I2C target (slave), address 0x2A, 100 and 400 kHz | 181 | `docs/examples-i2c-target.md` | `test_i2c_target.py` |
| `firmware/examples/spi_target.asm` | SPI target (slave), modes 0 and 3, up to about 1.3 MHz | 252 | `docs/examples-spi-target.md` | `test_spi_target.py` |
| `firmware/examples/spi_master_fast.asm` | SPI master, mode 0, 5 MHz SCK with the loadable schedule `0,1,0,2` (`spi_master_turbo.asm`: 8.33 MHz with two threads) | 93 | `docs/examples-fast-and-memory.md` | `test_spi_master_fast.py` |
| `firmware/examples/i2c_target_regs.asm` | I2C target (slave), address 0x2A, 16 read/write registers in the data memory, 100 and 400 kHz | 84 | `docs/examples-fast-and-memory.md` | `test_i2c_target_regs.py` |
| `firmware/examples/onewire.asm` | 1-Wire master: reset, READ ROM, CRC8 checked in firmware | 67 | `docs/examples-onewire-jtag.md` | `test_onewire_jtag.py` |
| `firmware/examples/jtag.asm` | JTAG master: read IDCODE, load BYPASS, measure chain length | 70 | `docs/examples-onewire-jtag.md` | `test_onewire_jtag.py` |
| `firmware/examples/ws2812.asm` | WS2812 LED strip driver, exact 800 kHz waveform from one thread | 90 | `docs/examples-leds-keyboard-midi.md` | `test_leds_keyboard_midi.py` |
| `firmware/examples/ps2_rx.asm` | PS/2 keyboard receiver, forwards scan codes to the UART | 64 | `docs/examples-leds-keyboard-midi.md` | `test_leds_keyboard_midi.py` |
| `firmware/examples/midi_tx.asm` | MIDI output, 31250 baud, running-status sequence | 48 | `docs/examples-leds-keyboard-midi.md` | `test_leds_keyboard_midi.py` |
| `firmware/examples/uart_rx.asm` | UART receiver | 29 | `docs/loading.md` | `test_example_uart_receiver` in `test.py` |

Two of these are controller roles that the ROM already had (SPI, I2C master); the I2C and SPI targets show
the other side of the same buses on the same hardware.

## Things to know before using them on a board

- **Pull-ups.** The I2C target needs pull-ups on SDA and SCL (as any I2C bus does) and the 1-Wire example needs
  one on the data line (as any 1-Wire bus does). Nothing else: the 1-Wire and JTAG programs use no scratch pads, only
  the pins of their protocol, and the test checks that the chip drives no other pad.
- **Pin conflicts.** `uio[5]` and `uio[4]` are the loader's LOAD and STROBE and are never used by a program.
- **Instruction set version.** Version 2 of the instruction set (`docs/isa.md`) added an ALU, branches on data
  and a small data memory. The 1-Wire and JTAG examples use them. The other examples were written for version 1
  (no add, XOR or branch-on-data), so they keep state in the program counter or move bits through an open-drain
  pad and read them back; each document explains where. Version 1 programs run unchanged.
- **Clock.** Timings assume the 50 MHz clock the design was built for.

## Building and loading a program

```
python3 tools/load_image.py build/spi_target firmware/examples/spi_target.asm
```

writes `build/spi_target.bin` (512 bytes in transmission order) and `.hex`; `docs/loading.md` explains the
pin sequence. The programs that need a faster thread schedule are built with
`python3 tools/load_image.py --sched 0,1,0,2 build/spi_fast firmware/examples/spi_master_fast.asm`, which appends
the schedule byte to the image. `tools/asm.py` is the assembler and `docs/isa.md` the instruction set.
