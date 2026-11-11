# Examples: 1-Wire master and JTAG master

Two more protocols that the chip was not built with. Both are loaded into the program memory
(`docs/loading.md`), neither is in the ROM, and each is well under 128 words (1-Wire 67, JTAG 70), so it also
fits half of the 256-word program memory. They are written for instruction set version 2 (`docs/isa.md`): the
CRC and the counters are real arithmetic, and the only pins they touch are the ones of the protocol. (The first
versions, written for version 1, which had no ALU and no branch on data, needed 101 and 94 words and three
extra open-drain pads that the firmware used as scratch.)

```
python3 tools/load_image.py build/onewire firmware/examples/onewire.asm
python3 tools/load_image.py build/jtag    firmware/examples/jtag.asm
cd test && make COCOTB_TEST_MODULES=test_onewire_jtag
```

Both report on UART TX, `uo[0]`, 115200 baud 8N1, using a transmitter thread (thread 1) that is part of the
file: the same transmitter as `uart_send` in `firmware/uart_tx.asm`, fed through the channel. Each program runs
once after it is loaded (or after a reset with LOAD held high) and then stops; `uo[0]` idles high. The programs
rely on the thread's data memory being zero after a reset (`docs/isa.md`), so they are not meant to be started a
second time without one.

## How version 2 is used

* **Bytes in true order.** Both buses send the LSB first. A byte is assembled in `A` with `SHR` (shift right,
  `C` = the bit that fell out) and, if the received bit is 1, `OR 0x80`: the first bit received ends in
  `A[0]`, so `A` holds the true byte and goes to the UART unchanged. `A` starts as `0x80`; that marker
  reaches `C` after exactly eight shifts and `JC` ends the byte, so no bit counter is needed. The two paths
  of the branch that tests the received bit (`JPIN`) take the same number of turns, so the timing does not
  depend on the data.
* **Counting.** 1-Wire counts its eight bytes in a data-memory cell in steps of `0x20`: the carry out of `ADD`
  after the eighth byte ends the loop. JTAG counts the 32 clocks of a scan in `X` with `DJNZ`, and the
  chain length is a plain binary count in `A` (`INC`; a wrap to 0 means "never came back").
* **Data memory** (private, 16 bytes per thread): the 1-Wire CRC register and byte counter, and the JTAG
  status.
* **Constants are the true values.** Status bytes are written as `0`, `1`, `2`; the chain length is sent as it
  is counted.

## 1-Wire master (`firmware/examples/onewire.asm`)

| Signal | Pin |
|---|---|
| DQ (1-Wire, open drain, external pull-up) | `uio[0]` (drive: pad 0, read: input pin 8) |
| UART TX | `uo[0]` |
| Program loader | `uio[5]`, `uio[4]` (not usable; LOAD low while running) |

No other pad is used or driven: `uio_oe` stays 0 except for bit 0, and the test checks it. The DQ line needs the
pull-up that every 1-Wire bus has; nothing else needs one.

Sequence: reset pulse, presence sample, `READ ROM` (0x33), 64 bits of ROM code (family code, 6-byte serial,
CRC byte). The ROM bytes go out on the UART as they arrive, then a status byte:

| UART output | Meaning |
|---|---|
| 8 bytes, `00` | CRC ok |
| 8 bytes, `01` | CRC error (the bytes are still reported) |
| `02` (one byte) | no presence pulse, or the bus did not return high after the reset slot |

The CRC is Dallas/Maxim CRC8, `x^8+x^5+x^4+1` (`0x8C` when the register shifts right), over all 8 bytes. The
register lives in `MEM[0]`. After the eighth bit of a byte, in the idle part of that same slot, the byte is
sent to the UART thread and the register is updated: `register ^= byte` (`XORX`), then eight times `SHR`, and if
a 1 fell out into `C`, `XOR 0x8C` (the two paths of that branch take the same number of turns too). The
register ends at zero exactly when the eighth byte equals the CRC of the first seven (for this CRC the
register after seven bytes XOR the next byte is zero iff they are equal), so there is no separate comparison:
the status is `LDM 0` and `JZ`.

Threads: thread 0 does the bus, the byte assembly, the CRC and the sequencing; thread 1 is the UART
transmitter and receives each byte through the channel. (The first version needed a third thread as a CRC
co-processor, because a thread had no way to hold the byte and the CRC register at once.)

Timing (turn = 80 ns):

| Item | Value | Standard speed |
|---|---|---|
| reset low | 6254 turns = 500.3 us | 480 us minimum |
| presence sample | 876 turns = 70.1 us after release | device answers 15-60 us after release, for 60-240 us |
| time released before first command | 6046 turns = 483 us | 480 us (tRSTH) |
| write 1 | low 75 turns = 6.0 us, slot 880 turns = 70.4 us | low 1-15 us, slot 60-120 us |
| write 0 | low 814 turns = 65.1 us, slot 880 turns = 70.4 us, recovery 66 turns = 5.3 us | low 60-120 us |
| read | low 75 turns = 6.0 us, sampled 151 turns = 12.1 us after the falling edge, slot 863 turns = 69.0 us | master must sample within 15 us |

All 64 read slots are 863 turns long, including the one that ends a byte: the CRC step (about 40 turns) and the
byte counter run in the idle part of the slot, so there is no gap between bytes.

Limits: standard speed only; no overdrive, no SEARCH ROM, only a single device on the bus (READ ROM with
several devices returns the wired-AND of their ROMs); a CRC error is reported but there is no retry.

## JTAG master (`firmware/examples/jtag.asm`)

| Signal | Pin |
|---|---|
| TCK, TMS, TDI (push-pull) | `uo[2]`, `uo[3]`, `uo[4]` |
| TDO | `ui[0]` (input pin 0) |
| UART TX | `uo[0]` |

No pad is driven at all: `uio_oe` stays 0 throughout, and the test checks it.

Sequence: five TCKs with TMS high (Test-Logic-Reset, which selects IDCODE), Run-Test/Idle, Shift-DR, 32 bits
shifted out with the last one on TMS high; bit 0 must be 1. Then Select-IR, 32 ones shifted in (every IR of
the chain becomes all ones, BYPASS, as long as the IR chain is at most 32 bits), Update-IR, Capture-DR (every
bypass bit loads 0), a single 1 shifted in on TDI and the TCKs counted until it comes out on TDO. The TAP is
left in Run-Test/Idle.

UART output, 6 bytes: IDCODE byte 0 (bits 0..7, the first ones out of the device), 1, 2, 3; status
(`00` IDCODE bit 0 was 1, `01` it was 0); chain length (number of devices; `FF` if the marker did not appear
within 255 TCKs, e.g. TDO dead). With several devices in the chain the IDCODE is that of the device nearest
TDO.

How it works:

* The paths between the scans (Test-Logic-Reset, Select-DR/IR, Update and so on) are TMS bit patterns in `A`,
  first bit in `A[0]`, with a 1 above the last bit as the end marker. The subroutine `jt_tms` gives one TCK per
  bit with `SHR` / `OUTC 3` until only the marker is left (`A` = 0). For example `0x3F` is five ones, `0x12` is
  0, 1, 0, 0.
* The two 32-bit scans count their TCKs in `X`. The last TCK of a scan needs TMS = 1; `XCMP 2` sets `C` to
  `X < 2`, and `OUTC 3` puts it on TMS, so TMS is 1 exactly when one TCK is left.
* The IDCODE bits are assembled into bytes as described above; the byte is sent to the UART thread as soon as
  it is complete. Bit 0 of the IDCODE is tested before the first TCK (TDO already shows it), and the result is
  kept in `MEM[0]` until the status byte is due.
* The chain length is counted in `A` with `INC` after each TCK of the search; TDO is tested with `JPIN`.
  When `A` wraps to 0 after 255 TCKs the program sends `0xFF`.

TCK: high 7 turns (560 ns), low at least 11 turns (880 ns), so the period is at least 18 turns = 1.44 us
(about 694 kHz). The low time is 11 to 20 turns depending on the code between two TCKs; it is stretched
further (up to about 2000 turns = 160 us in the run measured) where a byte has to wait for the UART thread
(`SEND` blocks while the previous byte is still in the channel). A TAP does not mind, TCK is static. TMS and
TDI change only while TCK is low, TMS at least 2 turns (160 ns) before the rising edge, and TDO is read at least
10 turns after the falling edge.

Limits: IR chain up to 32 bits; the chain length is counted in clocks, so a chain with a device that has no
bypass register, or a TDO that is high all the time, is not detected; only the first IDCODE is read.

## Tests (`test/test_onewire_jtag.py`)

Each program is loaded with the real pin loader and run against a Python device model.

* `OneWireSlave` decodes the bus from the pad. It checks the reset low time (480-960 us), the time before the
  first slot (480 us), slot length (at least 60 us), recovery (at least 1 us), write-1 low time (1-15 us),
  write-0 low time (60-120 us) and read-slot low time (1-15 us). It samples write slots at a configurable
  point in 15-60 us, answers only `0x33`, starts its presence pulse 15-60 us after the release for 60-240 us
  (the tests use both extremes), and for a 0 bit holds the line for exactly 15 us from the falling edge, so a
  master that samples later reads a 1. It also checks that the chip never drives any pad but `uio[0]`; the
  other pads read low, so nothing could rely on a pull-up.
* Cases: two good ROMs with extreme device timing; three kinds of CRC error (data byte, CRC byte, one bit of
  the last data byte); ROMs with all-zero and all-ones payloads and a CRC of 1; no device (status 2, master
  stops, no further pulses); bus stuck low (status 2).
* `JtagChain` is a chain of TAPs with the 16-state controller, IR capture of `...01`, IDCODE, BYPASS and
  shifting through several devices. It checks TCK period and pulse widths (1 MHz or slower), that TMS/TDI
  never change while TCK is high and are stable three clocks before the rising edge, that no pad is driven
  (`uio_oe` is 0 all the time), and records the TMS sequence, the TDI bits and the scans. The single-device
  test compares the complete TMS and TDI sequence with the expected one. Chains: one device with a 4-bit IR,
  two devices (4-bit and 5-bit IR), three and five devices with mixed IR lengths, and a dead TDO (status 1,
  length `FF`).
* The UART output is decoded with exact timing: every edge inside a frame must fall on a multiple of 432
  clocks after the start edge.

## Mutation checks

The module passes (10 of 10 tests, RTL simulation). To see that its checks are not vacuous, the final firmware was
broken on purpose in 21 ways, one at a time (a modified copy of the `.asm` file is given to the test through the
`OW_ASM` / `JTAG_ASM` environment variable), and the cheapest test that should notice was run on the RTL. Every
mutant fails:

| Firmware | Mutation | Test | What fails |
|---|---|---|---|
| 1-Wire | CRC polynomial `XOR 0x8C` -> `0x8D` | `test_onewire_good_crc` | status `01` for a good ROM |
| 1-Wire | CRC feedback branch `JC` -> `JNC` | `test_onewire_good_crc` | status `01` for a good ROM |
| 1-Wire | byte assembled MSB first (`SHL`, `OR 0x01`, marker `0x01`) | `test_onewire_good_crc` | bytes bit-reversed (`14` for `28`) |
| 1-Wire | status test `JZ` -> `JNZ` | `test_onewire_good_crc` | status `01` for a good ROM |
| 1-Wire | byte counter step `ADD 0x20` -> `0x40` | `test_onewire_good_crc` | stops after 4 bytes, UART output missing |
| 1-Wire | read sample point 12 us -> 21.5 us (`WAIT 73` -> 190) | `test_onewire_good_crc` | every bit reads 1 (device released its 0): all bytes `FF` |
| 1-Wire | write slot shortened (`WAIT 737` -> 100) | `test_onewire_good_crc` | slot 19.4 us < 60 us |
| 1-Wire | read slot shortened (`WAIT 707` -> 40) | `test_onewire_good_crc` | slot 15.7 us < 60 us |
| 1-Wire | reset pulse 500 us -> 400 us (`WAIT 2251` -> 1000) | `test_onewire_no_device` | reset low 400.2 us outside 480..960 |
| 1-Wire | drives scratch pad `uio[2]` (`ODRV 2 0`) | `test_onewire_no_device` | chip drives `uio_oe` = `04` |
| JTAG | TCK high 7 -> 3 turns (`WAIT 5` -> 1) | `test_jtag_single_device` | TCK high 240 ns < 400 ns |
| JTAG | IR scan 32 -> 30 clocks | `test_jtag_single_device` | scans `DR 32, IR 30, DR 2` |
| JTAG | last IDCODE clock without TMS = 1 (`XCMP 2` -> `XCMP 1`) | `test_jtag_single_device` | TAP does not leave Shift-DR, IR not BYPASS |
| JTAG | TMS pattern `1,1,1,0,0` -> `1,1,0,0,0` | `test_jtag_single_device` | IR not BYPASS |
| JTAG | IDCODE bit 0 test `JPIN 0 1` -> `JPIN 0 0` | `test_jtag_single_device` | status `01` for a good IDCODE |
| JTAG | IDCODE bytes assembled MSB first | `test_jtag_single_device` | bytes bit-reversed (`EE` for `77`) |
| JTAG | chain-length count step 1 -> 2 (`INC` -> `ADD 2`) | `test_jtag_single_device` | length `02` instead of `01` |
| JTAG | wrap test `JZ` -> `JNZ` | `test_jtag_single_device` | length `FE` instead of `01` |
| JTAG | IR scan shifts zeros (`PIN 4 1` -> `PIN 4 0`) | `test_jtag_single_device` | IR not BYPASS, length `FF` |
| JTAG | dead-TDO report `NOT` -> `INC` | `test_jtag_dead_tdo` | length `01` instead of `FF` |
| JTAG | drives scratch pad `uio[3]` (`ODRV 3 0`) | `test_jtag_single_device` | chip drives `uio_oe` = `08` |
