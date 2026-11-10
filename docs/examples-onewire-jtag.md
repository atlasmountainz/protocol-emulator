# Examples: 1-Wire master and JTAG master

Two more protocols that the chip was not built with. Both are loaded into the program memory
(`docs/loading.md`), neither is in the ROM, and each is under 128 words (1-Wire 101, JTAG 94), so it also
fits half of the 256-word program memory.

```
python3 tools/load_image.py build/onewire firmware/examples/onewire.asm
python3 tools/load_image.py build/jtag    firmware/examples/jtag.asm
cd test && make COCOTB_TEST_MODULES=test_onewire_jtag
```

Both report on UART TX, `uo[0]`, 115200 baud 8N1, using the thread-2 (1-Wire) or thread-1 (JTAG) transmitter
that is part of the file. Each runs once after the program is loaded (or after a reset with LOAD held high)
and then stops; `uo[0]` idles high.

## What the ISA forced, and how the programs deal with it

* **No ALU and no data memory.** A thread has `A`, `X` and `C`. The only data-dependent branch is `JPIN` on a
  pin. To branch on a bit of `A`, the programs shift it into `C`, put `C` on an open-drain pad with `ODRVC`
  and branch on the pad read back (`JPIN 9+n`). To put a bit into `A` they use `INB` from a pad.
  The scratch pads (`uio[1]`..`uio[3]`) are only ever driven by the chip, so they need no external part,
  but nothing may pull them low from outside, and the read-back must be fast enough (a pad that is released
  has to rise within a slot or two; with a plain pull-up or no connection that holds).
* **No XOR, no add.** The CRC is a shift register where the feedback is applied by rotating the register
  through a pad one bit at a time (`SHL`, `ODRVC`, `INB`). The JTAG chain-length counter is a binary counter
  kept bit-reversed, incremented the same way.
* **No way to read `X`, and `RECV` overwrites `A`.** A loop count therefore cannot be reported, and one
  thread cannot hold two byte-wide values. The 1-Wire example splits its state over three threads (below);
  the JTAG example builds its count in `A`.
* **Bit order.** `SHL` / `INB` shifts the first bit received into `A[7]`; nothing can insert at the top
  while shifting right. Both buses send the LSB first, so a received byte sits in `A` in *wire order*
  (reversed). Both programs send it to the UART with `SHL` / `OUTC`, which puts the first received bit
  (the LSB) on the line first, and the host sees the true byte. `uart_send` in `firmware/uart_tx.asm`
  shifts with `SHR`, which would send these bytes MSB first, so the examples carry a mirrored copy of it
  (same 108 slots per bit, `SHL` instead of `SHR`). Constant status bytes are written reversed in the
  source for the same reason (true `1` = `0x80`, true `2` = `0x40`).

## 1-Wire master (`firmware/examples/onewire.asm`)

| Signal | Pin |
|---|---|
| DQ (1-Wire, open drain, external pull-up) | `uio[0]` (drive: pad 0, read: input pin 8) |
| UART TX | `uo[0]` |
| STROBE, Q, ZERO (thread-to-thread scratch) | `uio[1]`, `uio[2]`, `uio[3]` |
| Program loader | `uio[5]`, `uio[4]` (not usable; LOAD low while running) |

Sequence: reset pulse, presence sample, `READ ROM` (0x33), 64 bits of ROM code (family code, 6-byte serial,
CRC byte). The ROM bytes go out on the UART as they arrive, then a status byte:

| UART output | Meaning |
|---|---|
| 8 bytes, `00` | CRC ok |
| 8 bytes, `01` | CRC error (the bytes are still reported) |
| `02` (one byte) | no presence pulse, or the bus did not return high after the reset slot |

The CRC is Dallas/Maxim CRC8, `x^8+x^5+x^4+1`. It is computed bit by bit as the data arrive, over all 64
bits. The register ends at zero exactly when the eighth byte equals the CRC of the first seven (for this CRC
the register after seven bytes XOR the next byte is zero iff they are equal), so there is no separate
comparison step.

Threads: thread 0 drives the bus, assembles each byte in `A` and sends it to the UART thread through the
channel. Thread 1 is a CRC co-processor: its `A` is the CRC register. At every sampled bit thread 0 pulls
STROBE (`uio[1]`) low for 8 slots; thread 1 sees it, looks at DQ itself 4 slots later, updates the register
and publishes "register is zero" on ZERO (`uio[3]`, low = zero). Thread 2 is the UART transmitter. The
two threads sample the line within about 0.5 us of each other, which is why the read sample point sits well
inside the device's 15 us window.

Timing (slot = 80 ns):

| Item | Value | Standard speed |
|---|---|---|
| reset low | 500.3 us | 480 us minimum |
| presence sample | 70.1 us after release | device answers 15-60 us after release, for 60-240 us |
| time released before first command | 483 us | 480 us (tRSTH) |
| write 1 | low 6.0 us, slot 70.4 us | low 1-15 us, slot 60-120 us |
| write 0 | low 65.1 us, slot 70.4 us, recovery 5.3 us | low 60-120 us |
| read | low 6.0 us, sampled 12.0 us after the falling edge (second sample by the CRC thread about 12.6 us), slot 69.0 us | master must sample within 15 us |

Limits: standard speed only; no overdrive, no SEARCH ROM, only a single device on the bus (READ ROM with
several devices returns the wired-AND of their ROMs); a CRC error is reported but there is no retry; the
three scratch pads must be left alone; the CRC thread relies on the data bit still being valid 4 slots
after STROBE, which the 15 us hold of a slave guarantees.

## JTAG master (`firmware/examples/jtag.asm`)

| Signal | Pin |
|---|---|
| TCK, TMS, TDI (push-pull) | `uo[2]`, `uo[3]`, `uo[4]` |
| TDO | `ui[0]` (input pin 0) |
| UART TX | `uo[0]` |
| scratch pads (carry/bit test, status flag) | `uio[2]`, `uio[3]` |

Sequence: five TCKs with TMS high (Test-Logic-Reset, which selects IDCODE), Run-Test/Idle, Shift-DR, 32 bits
shifted out with the last one on TMS high; bit 0 must be 1. Then Select-IR, 32 ones shifted in (every IR of
the chain becomes all ones, BYPASS, as long as the IR chain is at most 32 bits), Update-IR, Capture-DR (every
bypass bit loads 0), a single 1 shifted in on TDI and the TCKs counted until it comes out on TDO. The TAP is
left in Run-Test/Idle.

UART output, 6 bytes: IDCODE byte 0 (bits 0..7, the first ones out of the device), 1, 2, 3; status
(`00` IDCODE bit 0 was 1, `01` it was 0); chain length (number of devices; `FF` if the marker did not appear
within 255 TCKs, e.g. TDO dead). With several devices in the chain the IDCODE is that of the device nearest
TDO.

TCK: high 7 slots (560 ns), low at least 9 slots (720 ns), so the period is at least 1.28 us (about 780 kHz).
During the chain-length search the increment between TCKs stretches the low time (a few microseconds), which
a TAP does not mind. TMS and TDI change one slot (80 ns) before the TCK rising edge and only while TCK is
low; TDO is read about 7 slots after the falling edge.

Limits: IR chain up to 32 bits; the chain length is counted in clocks, so a chain with a device that has no
bypass register, or a TDO that is high all the time, is not detected; only the first IDCODE is read.

## Tests (`test/test_onewire_jtag.py`)

Each program is loaded with the real pin loader and run against a Python device model.

* `OneWireSlave` decodes the bus from the pad. It checks the reset low time (480-960 us), the time before the
  first slot (480 us), slot length (at least 60 us), recovery (at least 1 us), write-1 low time (1-15 us),
  write-0 low time (60-120 us) and read-slot low time (1-15 us). It samples write slots at a configurable
  point in 15-60 us, answers only `0x33`, starts its presence pulse 15-60 us after the release for 60-240 us
  (the tests use both extremes), and for a 0 bit holds the line for exactly 15 us from the falling edge, so a
  master that samples later reads a 1.
* Cases: two good ROMs with extreme device timing; three kinds of CRC error (data byte, CRC byte, one bit of
  the last data byte); ROMs with all-zero and all-ones payloads and a CRC of 1; no device (status 2, master
  stops, no further pulses); bus stuck low (status 2).
* `JtagChain` is a chain of TAPs with the 16-state controller, IR capture of `...01`, IDCODE, BYPASS and
  shifting through several devices. It checks TCK period and pulse widths (1 MHz or slower), that TMS/TDI
  never change while TCK is high and are stable three clocks before the rising edge, and records the TMS
  sequence, the TDI bits and the scans. The single-device test compares the complete TMS and TDI sequence
  with the expected one. Chains: one device with a 4-bit IR, two devices (4-bit and 5-bit IR), three and five
  devices with mixed IR lengths, and a dead TDO (status 1, length `FF`).
* The UART output is decoded with exact timing: every edge inside a frame must fall on a multiple of 432
  clocks after the start edge.
