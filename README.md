![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# Protocol emulator: a tiny cycle-exact CPU for bit-banging serial protocols

An entry in Jane Street's [protocol emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/),
built for Tiny Tapeout on IHP's 130 nm CMOS5L process.

Instead of one fixed block per protocol, the chip is a small CPU whose instructions are built for
reading pins, driving pins and counting cycles. Protocols are **firmware**. The same silicon runs
three protocols today, picked by a mode pin at boot:

| Mode pin | Protocol | What the demo firmware does |
|---|---|---|
| none | **UART** TX, 8N1, 115200 baud @ 50 MHz | sends the byte on `ui[7:0]` |
| `uio[7]` | **SPI** master, mode 0, MSB first | sends `ui[7:0]`, reads a byte, sends it back (self-test) |
| `uio[6]` | **I2C** master, 100 kHz, clock stretching | EEPROM random read at address 0x50, **printed on the UART**: a protocol bridge |

The I2C firmware calls the UART firmware as a subroutine, so one protocol feeding another is just a
`CALL`.

## Design in one page

```
 ui[7:0] ─┐                       ┌────────────┐
 uio[7:0] ┴─> 2-flop sync ─pins─> │  CPU core  │ ──push-pull──> uo[7:0]
                                  │ A X C PC   │ ──open-drain─> uio[7:0]  (ODRV: pull low / release)
                                  │ 4-deep call│
                                  │ stack      │ <──instr── ROM (256 x 16, generated from firmware/*.asm)
                                  └────────────┘
```

- **Cycle-exact.** Every instruction takes 1 clock, except `WAIT n` which takes `1 + n`. Protocol
  timing is a matter of adding up cycles in the program, with no hidden pipeline effects.
- **Small ISA, 19 instructions** (`docs/isa.md`): `PIN`, `OUT`, `WAIT`, `JMP`, `JPIN`, `LDA`, `SHR`,
  `SHL`, `OUTC`, `LDX`, `DJNZ`, `IN`, `INB`, `CALL`, `RET`, `ODRV`, `ODRVC`, `NOP`, `HALT`.
- **Open-drain pins.** `ODRV` pulls a line low or releases it, and the pad can always be read back,
  so a slave stretching the I2C clock is visible to the firmware.
- **Shift through a carry flag.** `SHL` / `SHR` move one bit into `C`; `OUTC` / `ODRVC` drive a pin
  from `C`; `INB` samples a pin into `A[0]`. Together these make a serial shift loop 4 instructions.
- **Firmware toolchain.** `tools/asm.py` is a small assembler with labels. `tools/build_rom.sh`
  assembles `firmware/*.asm` into `src/rom.v`.

Pinout and test instructions are in [`docs/info.md`](docs/info.md); the instruction set is in
[`docs/isa.md`](docs/isa.md).

## Verification

The tests are layered so that they can catch different kinds of mistakes:

1. **Directed cocotb tests** (`test/test.py`, run by CI): UART idle and data, SPI against a
   simulated slave, I2C read / missing-slave / clock-stretching against a simulated EEPROM.
2. **Cycle-exact timing.** The UART test asserts every TX transition is exactly 434 cycles apart.
   It caught a real firmware bug (a stop bit starting one cycle early).
3. **Protocol-spec checks.** The simulated I2C bus measures the master against the standard-mode
   limits (tLOW, tHIGH, tHD;STA, tSU;STA, tSU;STO, tBUF, tSU;DAT) on every edge.
4. **Mutation checks.** Each test suite was run against deliberately broken firmware or RTL to confirm
   it fails: shortened I2C delays, a removed clock-stretch wait, a wrong DJNZ exit, a wrong RET slot,
   an inverted `ODRVC`, a dropped JPIN address bit. Each was caught.
5. **Random differential testing** (`test/random_run.py`): random programs and random pin inputs run
   on the RTL and on an independent Python model of the ISA (`test/isa_model.py`, written from
   `docs/isa.md`), comparing PC, A, X, C, stall counter, stack pointer and every output pin **on every
   clock edge**. It also reports instruction and branch-outcome coverage.
6. **CI.** GitHub Actions runs the cocotb tests, builds the layout (LibreLane), and runs a
   gate-level simulation of the synthesized netlist.

```
cd test && make                      # directed tests
cd test && python3 random_run.py 100 # 100 random programs vs the ISA model
```

## Hardware results

Measured by the LibreLane build in CI (6x4 tiles, CPU with UART firmware):

| | |
|---|---|
| Cells after place and route | 778 (including clock tree and hold buffers) |
| Standard-cell area | about 11,600 µm² |
| Core area available (6x4) | 902,417 µm² (about 1.3% used) |
| Setup timing | no violations at the target clock |
| Antenna violations | 0 |

The design uses a tiny fraction of the allowed area, so there is a lot of headroom for more
capability (see below).

## Repository layout

```
src/            project.v (top), cpu.v (core), rom.v (generated)
firmware/       main.asm (boot dispatcher), uart_tx.asm, spi_master.asm, i2c_master.asm
tools/          asm.py (assembler), build_rom.sh
test/           directed tests, ISA model, random differential test
reference/      the original fixed-logic UART, kept as a golden model
docs/           info.md (datasheet), isa.md (instruction set)
```

## Status and ideas

- Working today: UART TX, SPI master, I2C master, all verified in simulation; layout builds.
- Not done yet: a program memory that can be loaded after fabrication (today the firmware is in ROM),
  receive-side UART, and more protocols (JTAG, SWD, PS/2, CAN, low-speed USB, 10BASE-T). 
- The core is only about 1% of the allowed area, so the next step is to have pretty much a writable program
  memory plus several cores sharing the pins.

## About the development

This project was developed with some help from an AI assistant (Claude, by Anthropic), which
helped with some of the RTL, firmware, assembler, and tests under the author's direction. Everything here is
open source (Apache-2.0).

<!-- TODO(author): add your own notes here: why you chose this design, what you learned. -->
