![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# Protocol emulator: one chip, a bunch of protocols, all in software

Hello team. This is my entry for Jane Street's
[protocol emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/). It's a
chip for Tiny Tapeout on IHP's 130 nm process (8x4 tiles, 50 MHz clock).

The challenge: make a chip that can speak protocols it wasn't designed for. So instead of building a UART block, an
SPI block, an I2C block, and so on, I decided to build a tiny CPU that's really good at wiggling pins at exact times, and every
protocol is just a program. You load a new program over the pins and the same chip becomes something else. No new
silicon needed.

## What it can do

The chip boots with UART, SPI and I2C already in its ROM. Everything else is a program you load after it's made:

| Protocol | What the program does | Doc |
|---|---|---|
| UART | send, receive, and full-duplex echo | [info](docs/info.md) |
| SPI | master (up to 8.33 MHz) and target | [fast SPI](docs/examples-fast-and-memory.md), [target](docs/examples-spi-target.md) |
| I2C | master (with clock stretching) and target with 16 registers | [target](docs/examples-i2c-target.md) |
| 1-Wire | reads a device's ROM code and checks its CRC | [1-Wire / JTAG](docs/examples-onewire-jtag.md) |
| JTAG | reads the first device's ID and counts the devices in the chain | [1-Wire / JTAG](docs/examples-onewire-jtag.md) |
| SWD | wakes up an ARM debug port and powers it on | [SWD / CAN](docs/examples-swd-can.md) |
| CAN | receives 500 kbit/s frames, checks the CRC, sends the ACK | [SWD / CAN](docs/examples-swd-can.md) |
| Low-speed USB | receives 1.5 Mbit/s packets, checks PIDs and CRCs | [USB](docs/usb-low-speed.md) |
| WS2812, PS/2, MIDI | LED strip driver, keyboard receiver, MIDI out | [LEDs / keyboard / MIDI](docs/examples-leds-keyboard-midi.md) |

That's every protocol on Jane Street's list except 10 Mbit Ethernet, which just doesn't fit in a 50 MHz chip. Some are
only half done, and I explain that in [What it can't do](#what-it-cant-do).

## How it works (the short version)

The core is a **four-thread barrel CPU**. There are four threads, and they take turns in a fixed order, one
instruction each. Every instruction takes exactly one turn (or `1 + n` turns for `WAIT n`). Nothing else can slow a
thread down: no interrupts, no caches, no fighting over the bus. So the timing of a protocol is just counting
instructions, and two protocols can run side by side without messing each other up.

Some other things it has:

- **Pin instructions.** Set a pin, wait, jump if a pin is high or low, and shift a bit out through the carry flag.
- **Open-drain pins** that can only pull low or let go, so I2C, 1-Wire and SWD work without ever fighting the
  other device.
- **An 8-bit ALU, 16 bytes of memory per thread, and jump tables**, so CRCs and parity get computed in the program.
- **A mailbox between threads.** For example, one thread reads the bus and another sends the bytes out over UART.
- **A 256-word program memory you load over the pins** (`tools/load_image.py` makes the file). See
  [loading](docs/loading.md).
- **A schedule you can change**, so one thread can get two of the four turns and run twice as fast. CAN and USB use
  this.

The full instruction set is in [docs/isa.md](docs/isa.md).

## The part I think is coolest: proving the timing

Usually you test protocol timing by simulating it and hoping you hit the bad cases. Because every instruction on this
chip takes a fixed number of clocks, I could do something stronger. [`tools/timing.py`](tools/timing.py) is a static
timing analyzer for the firmware. It looks at every possible path through a program, for every possible input, and
tells you the exact shortest and longest time between two events. Then you write the protocol's rules down
(like "SCL low for at least 4.7 µs") and it proves them or tells you which path breaks them.

Some things it proved:

- The I2C master never has SCL low for less than the spec, on any path. It also found a real bug: SDA could change
  160 ns after SCL fell, which is too fast. I fixed it and now it's proven to be at least 320 ns.
- The USB receiver re-lines-up with the sender after exactly 12 turns on every single path, which is the whole reason
  it can follow a 1.5 Mbit/s signal without a PLL.
- The CAN ACK is exactly one bit long and lands in the ACK slot.

To make sure the analyzer itself isn't lying, I checked it against the actual chip: the RTL runs every program
against random traffic, and all 8,905 measured intervals land inside the proven ranges. When I broke the analyzer on
purpose, the check caught it. More in [docs/timing-analysis.md](docs/timing-analysis.md).

## How I tested it

I didn't think one test was sufficient validation, so I built more layers:

1. **Formal proofs** that every instruction takes one turn and that threads can't mess with each other's timing.
2. **Random programs** run on the RTL and on a separate Python model of the CPU, comparing every register on every
   clock.
3. **Protocol tests** against models of the other device (an EEPROM, a USB host, a CAN bus, an ARM debug port, ...),
   with the bit rate pushed off and jitter added.
4. **Mutation tests**: I break the firmware on purpose and make sure the tests notice.
5. **Timing proofs** with the analyzer above.
6. **CI** on every push: the Tiny Tapeout build (layout, timing, DRC, LVS, precheck) plus the tests on the
   gate-level netlist.

Everything is in [docs/verification.md](docs/verification.md).

```
cd test && make                                   # the main tests on the RTL
python3 -m pytest test -q                         # the Python tests (model, analyzer, protocols)
python3 tools/timing.py firmware/timing/*.timing  # the timing proofs
```

## Hardware numbers

From the CI build: 35,599 cells, about 49 % of the 8x4 area, and no setup or hold violations at 20 ns in any corner.
Antenna, LVS, DRC and precheck all pass. Most of the area is the 256-word program memory. More in
[docs/results.md](docs/results.md).

## What it can't do

The truth about its limitations:

- **USB only receives.** The bidirectional pins can only pull low (open drain), so the chip can't drive both
  levels on the USB lines. Sending would need an outside driver chip.
- **CAN receives and acknowledges, but doesn't send frames.** It only does standard 11-bit IDs.
- **No Ethernet.** 10 Mbit Manchester needs way more speed than a 50 MHz chip running one instruction every 40 ns.
- **The other end is always a model I wrote.** The USB host, CAN bus, debug port, and so on are all simulations. None
  of this has run on a real device or on silicon yet, unfortunately.
- **It doesn't fit the Tiny Tapeout FPGA board** (it needs about twice the logic), so I couldn't test it on an FPGA.

## About the development

This project was developed with some help from an AI assistant (Claude, by Anthropic), with some additional assistance from friends and colleagues, who
helped with some of the RTL, firmware, assembler, and tests under the author's direction. Everything here is
open source (Apache-2.0).

<!-- TODO(author): add your own notes here: why you chose this design, what you learned. -->

## Repo map

```
src/        the chip (cpu.v is the core)
firmware/   the ROM programs, examples/ (loadable programs), timing/ (timing specs)
tools/      assembler, image builder, timing analyzer
test/       all the tests and protocol models
docs/       everything in more detail
```

Everything is open source under Apache-2.0.
