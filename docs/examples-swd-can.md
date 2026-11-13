# Examples: SWD host and CAN receiver

Two more protocols from the organisers' list that the chip was not built with. Both are firmware for the loadable
program memory (`docs/loading.md`); the silicon is unchanged.

| Program | What it does | Words | Schedule |
|---|---|---|---|
| `firmware/examples/swd.asm` | SWD host: line reset, JTAG-to-SWD, read DPIDR, power up the debug domain, report on the UART | 173 | default |
| `firmware/examples/can_rx.asm` | CAN 2.0A node, receive side, 500 kbit/s: destuffing, CRC-15, ACK | 192 | `0,1,0,1` |

```
python3 tools/load_image.py build/swd firmware/examples/swd.asm
python3 tools/load_image.py --sched 0,1,0,1 build/can_rx firmware/examples/can_rx.asm
cd test && python3 -m pytest test_swd_model.py test_can_model.py     # on the instruction-set model, seconds
cd test && make COCOTB_TEST_MODULES=test_swd_can_rtl                 # on the chip (RTL; gate level in CI)
python3 tools/timing.py firmware/timing/swd.timing firmware/timing/can_rx.timing firmware/timing/can_rx_thread1.timing
```

## SWD host (`firmware/examples/swd.asm`)

| Signal | Pin |
|---|---|
| SWCLK | `uo[2]` (push-pull, idles low) |
| SWDIO | `uio[0]` (open drain, read back as input pin 8); needs a pull-up of 4.7 kΩ or less |
| UART TX (the report) | `uo[0]`, 115200 baud 8N1 |

SWDIO is one wire that both ends drive in turn. The pad is open drain, so the host only pulls the line low or lets
go: a 1 is the pull-up, and the host can never fight the target. The price is speed (the pull-up has to raise the
line), which is why SWCLK runs at 431 kHz at most.

What the program does, once, after it is loaded:

1. Line reset (56 clocks with SWDIO high), the JTAG-to-SWD select sequence `0xE79E`, line reset, 8 idle clocks.
2. Read `DPIDR` (the first transaction after a line reset has to be this one).
3. Write `ABORT = 0x0000001E` (clear the sticky errors).
4. Write `CTRL/STAT = 0x50000000` (request power for the debug and system domains).
5. Read `CTRL/STAT`: a debug port that powered up answers `0xF0000000`.

Each transaction is 8 request bits, a turnaround, 3 ACK bits, then 32 data bits and a parity bit in one direction or
the other, all LSB first. The firmware computes the parity for both directions (it checks the target's and makes its
own), repeats a request up to 16 times while the target answers WAIT, and stops after the report if the final answer
is not OK.

UART report: for a read the ACK (1 = OK, 2 = WAIT, 4 = FAULT, 7 = nobody answered), then, if OK, the four data bytes
(low byte first) and `00` (parity right) or `01` (wrong); for a write the ACK. A working debug port gives 14 bytes:

```
01 77 14 A0 2B 00   01   01   01 00 00 00 F0 00
   DPIDR 0x2BA01477      ABORT     CTRL/STAT = 0xF0000000
```

The bytes go to a UART thread through the mailbox. The UART is far slower than the wire, so the SWD thread often
waits for it; it does that only while SWCLK is low, and since the host owns the clock the protocol does not mind.

**Proved for every execution** (`firmware/timing/swd.timing`, 36,937 abstract states), without assuming anything
about what the target answers or when the UART is free:

| | Proved |
|---|---|
| SWCLK high | exactly 12 turns (960 ns) |
| SWCLK low | at least 17 turns (1.36 µs), so at most 431 kHz |
| a host bit on SWDIO before the rising edge that the target samples it on | 1.12 to 1.76 µs (room for the pull-up) |
| the host never moves SWDIO while SWCLK is high | at least 1.2 µs from a rising edge to the next change |
| a target bit is read | exactly 1 turn before a rising edge, at least 2.24 µs after the rising edge that put it on the line |

## CAN receiver (`firmware/examples/can_rx.asm`)

It needs a CAN transceiver between the bus and the pins (the rules put transceivers on a Pmod).

| Pin | Direction | Meaning |
|---|---|---|
| `ui[0]` | in | RXD from the transceiver (1 = recessive) |
| `ui[7]` | in | 1 = listen only: never drive TXD |
| `uio[3]` | out, open drain | TXD to the transceiver: pulled low for one bit to acknowledge a frame |
| `uo[7:0]` | out | the report byte, valid while BYTE or STATUS is low |
| `uio[0]` BYTE | out, open drain | low for 160 ns for every report byte |
| `uio[1]` STATUS | out, open drain | low for 160 ns once at the end of every frame |
| `uio[2]` EVENT, `uio[6]` LAST | internal | flags between the two threads, read back through the pads |

All five `uio` pads are open drain and each needs a pull-up (10 kΩ; TXD may use the transceiver's own).

For every standard frame the chip reports `ID[10:9]`, `ID[8:1]`, a byte with `ID[0]`, RTR, IDE, r0 and `DLC[3:0]`,
the data bytes, and `CRC[14:7]`; then a status: `0` good frame (and acknowledged, unless listen-only), `1` CRC error,
`2` six dominant bits (an error flag, or a stuffing error), `3` six recessive bits where the frame should have gone
on. Example: identifier `0x123` with the data `11 22 33` is reported as `00 91 83 11 22 33 CB`, status `00`.

### How it works

**Thread 0 is the line receiver, and it does not sample bits in the middle.** In NRZ with bit stuffing the value of a
bit follows from whether the level changes at the bit boundary. The thread polls RXD with 13 `JPIN` in a row around
the place where the boundary is expected (±240 ns). An edge in the window means the new bit has the other level,
and the next window starts exactly 44 turns after the poll that saw it, so the receiver re-aligns to the transmitter
at every edge without a PLL (stuffing guarantees an edge at least every 5 bits). No edge means the same level again,
and the next window starts exactly 50 turns (one bit) after this one. The bus level is the program counter: one copy
of the window for each level. `X` counts equal bits in a row: after five, the next bit is a stuff bit and is dropped;
a sixth equal bit ends the frame.

Every bit goes through the CRC-15 (polynomial `0x4599`, kept shifted left by one in two memory cells, 13 turns per
bit with both branches the same length) and into an 8-bit chunk for thread 1. The first chunk after the start bit is
made one bit long. That choice puts the end of the last chunk exactly on the last CRC bit, for every data length,
which is what makes the ACK possible: when the last chunk ends, the CRC register is zero or the frame is bad.

**The ACK.** Thread 1 reads the DLC, counts chunks, and pulls the LAST flag low while the last chunk is on its way.
Thread 0 sees the flag when that chunk ends, checks that the CRC register is zero, waits for the ACK slot (one bit
later, two when the CRC ended with five equal bits and a stuff bit follows) and pulls TXD low for exactly one bit.
The timing is thread 0's own: a fixed number of turns from the bit that ended the frame.

**Thread 1 is the protocol.** The chunks are one bit off the byte boundaries of the frame, so each report byte is
the previous chunk shifted left with the top bit of the new one (`SHL`, `ROL`: four instructions).

### What is proved about its timing

`firmware/timing/can_rx.timing` (thread 0, 603,908 abstract states) and `can_rx_thread1.timing`. The chunk and the CRC
are data that timing must not depend on; the spec tells the analyzer to forget them at every window
(`forget ... at`, `docs/timing-analysis.md`), so each line holds for every bit sequence and every CRC value.

| | Proved |
|---|---|
| from the poll that saw an edge to the first poll of the next window | exactly 44 turns, on every path (start of frame included) |
| a window | 13 polls, one turn apart |
| after a window without an edge, the next window starts | exactly 50 turns (one bit) after this one started |
| the ACK | exactly 50 turns (2 µs) long |
| from the poll that saw the edge of the last CRC bit to the ACK | exactly 99 turns, or 149 with a stuff bit after the CRC (the bit began about a turn before that poll, so the ACK starts two or three bit times after it, as it must) |
| the same when the last CRC bit had no edge | 93 or 143 turns after the last poll of its window, which is 99 or 149 after the middle poll |
| thread 1 handles one message | in at most 42 turns (1.68 µs); the report strobes are exactly 4 turns |

The mailbox holds one byte and `SEND` waits if it is full, which would break thread 0's timing. It does not happen:
two chunks are at least 8 bits apart (the analyzer's lower bound for a single bit is 44 turns, more than the 42 turns
thread 1 needs for anything), a status code follows a chunk by 11 turns at the closest but finds the mailbox
empty because the chunk was taken at once, and the next chunk after a status is at least 79 turns away (two status codes: 68).

`test/test_timing_rtl.py` checks these specs against the RTL: 1,529 intervals of thread 0 and 248 of thread 1 in a
run against random traffic with errors, all inside the proved ranges, the exact ones exact.

### Tolerance (measured on the instruction-set model)

Against the waveform of `test/can.py`, with worst-case stuffing patterns (all 0, all 1, alternating) and random
frames back to back: every frame is received and acknowledged with the bit time from **4.5 % shorter to 2.0 %
longer** than 2 µs. The range is not symmetric: an edge that comes early is still seen by the first poll of the
window, a late one is missed. With the bit time 1 % off, edge jitter of ±4 clocks (80 ns) is tolerated. In runs
within ±1.5 % with ±2 clocks of jitter the ACK started within 160 ns of the true start of the slot. These are
simulation results against the model in this repository, not measurements on a CAN bus.

## How they are tested

| What | Where |
|---|---|
| the debug-port model against a plain reference host in Python; the request bytes | `test/test_swd_model.py` |
| the SWD firmware on the instruction-set model: the whole sequence with random DPIDR values of both parities, a target already in SWD mode, nobody there, WAIT (1, 3, 15 times, and for ever), FAULT, a wrong parity bit in each read, write data of both parities, the timing the target sees, 8 mutants of the firmware | `test/test_swd_model.py` (34 tests) |
| the CAN model: CRC-15 against the catalogue check value `0x059E`, stuffing, frame layout | `test/test_can_model.py` |
| the CAN firmware on the instruction-set model: known frames, random frames back to back at ±1 % with jitter, every DLC, remote frames, a stuff bit after the CRC, listen-only, another node acknowledging, transceiver loop delay, wrong CRCs, every single-bit data error, error flags, a frame that stops, extended frames (not acknowledged), a busy bus at start-up, the tolerance range, 10 mutants | `test/test_can_model.py` (42 tests) |
| both on the chip's RTL, and on the gate-level netlist in CI, pins only | `test/test_swd_can_rtl.py` (4 tests) |
| the analyzer's ranges against the RTL | `test/test_timing_rtl.py` (`test_swd_loaded`, `test_can_rx_thread0_loaded`, `..._thread1_loaded`) |

## Limits

- **Both bus models are this repository's own**, written from the specifications as remembered (the SWD select
  sequence and request layout were checked against OpenOCD's header, the transaction order follows the CMSIS-DAP
  probe, the CAN CRC against the catalogue value). Neither program has talked to a real debug port, a real CAN
  controller or a transceiver, and no silicon has run them.
- SWD: the program is a fixed sequence on the debug port (DP) only. Reading memory needs access-port transactions,
  which the same routines can do but the example does not. No dormant state, no multi-drop (SWD version 2).
- CAN: it receives and acknowledges. It does not transmit frames, does not send error flags or overload frames and
  keeps no error counters, so it is a listener that acknowledges, not a full node. Standard 11-bit frames only: an
  extended frame is reported with status 1 and not acknowledged.
- CAN: a bit's value comes from the edge at its boundary, not from a sample at 75 % of the bit as in a CAN
  controller. A glitch near a boundary is taken as an edge; there is no filtering.
- CAN: it re-aligns on edges in both directions (a CAN controller uses only recessive-to-dominant edges), so the
  difference between a transceiver's two edge delays eats into the ±240 ns window.
- CAN: 500 kbit/s is set by the constants (50 turns per bit). 250 and 125 kbit/s need longer waits; 1 Mbit/s does
  not fit, because the work per bit takes 29 of the 25 turns a bit would have.
- The CAN receiver uses 192 of 256 words: there is no room for a transmitter in the same image.
