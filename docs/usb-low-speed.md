# Low-speed USB (1.5 Mbit/s): a host and a receiver

Two firmware programs for the loadable program memory; nothing in the hardware knows about USB.

- `firmware/examples/usb_ls_host.asm` (217 words) is a **host**: it resets the bus, sends keep-alives, sends the first
  transaction of every enumeration (SETUP + DATA0 with GET_DESCRIPTOR(device) to address 0), turns the bus around and
  receives the device's handshake. It drives D+ and D- itself, so it needs the pads' push-pull mode (`PPA`,
  `docs/isa.md`). See [the host](#the-host) below.
- `firmware/examples/usb_ls_rx.asm` (246 words) is a **receiver**: it receives and checks any low-speed packet (SYNC,
  NRZI, bit unstuffing, PID check, CRC5 on tokens, CRC16 on data packets) and reports it. It only listens.

## The host

```
python3 tools/load_image.py --sched 0,1,0,1 build/usb_ls_host firmware/examples/usb_ls_host.asm
```

| Pin | Meaning |
|---|---|
| `uio[0]` | D+ (driven while the host sends, released while the device answers; read back as input 8) |
| `uio[1]` | D- (the same; input 9) |
| `uo[7:0]` | the result of the last transaction: the device's handshake PID (`D2` ACK, `5A` NAK, `1E` STALL), `00` if no device answered within 17 bit times, `FF` if the answer did not start with a SYNC |

The board needs what a host port has: 15 kΩ pull-downs on D+ and D- (the device brings its own 1.5 kΩ pull-up on
D-), and, for a real cable, the series resistors and 3.3 V signalling of a USB port.

What it sends: SE0 for 10.5 ms (bus reset), then once per millisecond a keep-alive (an EOP on its own), then
`2D 00 10` (SETUP, address 0, endpoint 0) and `C3 80 06 00 01 00 00 40 00 DD 94` (DATA0 with the 8-byte request),
then it listens for the handshake. Address 0, endpoint 0 is the only address a device answers before it has been
given one, so this is the request every host sends first. The two packets are constants in the program; their
CRCs were taken from the reference model (`test/usb_ls.py`), which checks them against the catalogue values.

### How it sends

**The pads.** `PPA 0x03` puts D+ and D- in push-pull mode; `ODRVA` then writes both lines in one instruction:
`0xFE` = J, `0xFD` = K, `0xFC` = SE0, so there is never a moment with one line changed and the other not. To let go,
`PPA 0` returns the pads to open drain (D+ is still pulled low, D- is held high by the device's pull-up: still J)
and `ODRVA 0xFF` releases D+, which stays low on the host's pull-down. No glitch, no SE1.

**Two threads.** Thread 1 is the encoder: it does the bit stuffing and passes the bits to thread 0 through the
mailbox, 7 at a time, as a byte with a 1 above the bits (so thread 0 sees when a group is used up without a counter).
`0x00` ends a packet. Thread 0 is the line driver: NRZI (a 0 toggles J and K) and one `ODRVA` per bit cell.

**Exact cells.** A bit is 16.67 turns of 40 ns. The cells are 17, 17 and 16 turns (680, 680, 640 ns), so three bits
are exactly 2 µs and the average rate is exactly 1.5 Mbit/s, without drift, however long the packet. Each cell is one
call of the same routine, and the level is written 13 turns after the call on every path: a 0 or a 1, a new group
from the mailbox or not. The end of the packet comes out of the same path, so the SE0 also starts on the grid.

### What is proved about its timing

`firmware/timing/usb_ls_host.timing` (thread 0, 282,344 abstract states) and `usb_ls_host_enc.timing` (thread 1):

| | Proved |
|---|---|
| one group of three cells | exactly 50 turns (2 µs) |
| a level written | exactly 13 turns into the group, then 17, 17, 16 turns apart, on every path |
| the last cell to the SE0 of the EOP | 16 or 17 turns, like any other cell |
| EOP | SE0 for exactly 1.32 µs (USB: 1.25 to 1.5 µs), then J for exactly 17 turns, then the lines are let go |
| handshake samples | 16 or 17 turns apart (16.67 on average) |
| the encoder | makes a group of 7 bits in at most 4.52 µs, faster than the driver uses one (4.67 µs) |

The mailbox holds one group and the encoder works on the next, so the driver never waits; the tests check this on
every edge of every packet. `test/test_timing_rtl.py` (`test_usb_ls_host_thread0_loaded`) runs the host on the RTL
against the device model: all 702 intervals are inside the proved ranges and every range is hit exactly.

### Jitter, against the USB specification

Every transition is between 0 and 26.7 ns after its place on the ideal 666.67 ns grid, and the bit rate is exact
on average (USB asks a host for 1.5 Mbit/s ± 0.05 %). The USB 2.0 limits for a host's low-speed source jitter are
±25 ns to the next transition and ±14 ns for a pair of transitions; the cells measured here are +13.3, +13.3 and
−26.7 ns from the ideal one, so the host is **outside those two limits by 1.7 ns and 12.7 ns**. That comes from the
40 ns instruction step: no single thread can do better. A device's own limits (±95 / ±150 ns) and the receive
tolerances of devices are much wider, so a device should not notice, but this is a deviation from the specification
and is stated as one. Placing the edges on 20 ns steps would need the two threads to take turns writing the cells.

### How the host is tested

| What | Where |
|---|---|
| on the instruction-set model against the device model (`test/usb_host_bus.py`): the packets decoded with the reference model, the bus reset, keep-alives, EOPs, the gap between token and data, the release of the lines, every transition against the grid, ACK / NAK / STALL / no answer / a garbled answer, the device answering 2 to 6.5 bit times later at -1.5 %, 0 and +1.5 %, other requests and addresses (all 1s: a stuffed bit after every six), 8 mutants of the firmware | `test/test_usb_ls_host_model.py` (35 tests) |
| the same on the chip's RTL, and on the gate-level netlist in CI | `test/test_usb_ls_host_rtl.py` (2 tests, pins only) |
| the analyzer's ranges against the RTL | `test/test_timing_rtl.py` (`test_usb_ls_host_thread0_loaded`) |

### Limits of the host

- One fixed transaction: it does not go on to read the descriptor (an IN token and a DATA1 packet back), give the
  device an address or configure it. Receiving a data packet needs the receiver below, and the two do not fit in
  256 words together.
- The packets are constants; the CRCs are not computed on the chip (the receiver computes both kinds).
- It does not check the device's EOP, the handshake's PID check bits or the bus state before it sends.
- The jitter deviation above.
- The device is this repository's model. It was not tried with a real USB device, and no silicon has run it.

# The receiver

## What it does

```
python3 tools/load_image.py --sched 0,1,0,1 build/usb_ls_rx firmware/examples/usb_ls_rx.asm
```

The schedule `0,1,0,1` (`docs/loading.md`) gives threads 0 and 1 one instruction every 2 clocks (40 ns). A USB bit
(666.67 ns) is 16.67 of these turns.

| Pin | Direction | Meaning |
|---|---|---|
| `ui[0]` | in | D+ |
| `ui[1]` | in | D- |
| `uo[7:0]` | out | the report byte, valid while BYTE or STATUS is low |
| `uio[0]` BYTE | out, open drain | low for 160 ns for every received byte |
| `uio[1]` STATUS | out, open drain | low for 160 ns once at the end of every packet |
| `uio[2]` EVENT | internal | thread 0 pulls it low to tell thread 1 that the next mailbox byte is an event, not data; thread 1 reads the pad back. It is visible on the pad and works as an end-of-packet indicator |

The three `uio` pads are open drain, so each needs a pull-up resistor (10 kΩ to the I/O supply, as for I2C): BYTE and
STATUS so that the consumer sees a high level, and EVENT because the firmware reads its own pull-down back through the
pad. Without the pull-up on `uio[2]` thread 1 would see every byte as an event.

For every packet the chip reports each byte after SYNC as it arrives (the PID, the payload, the CRC bytes, exactly as
received), then one **status byte** after the end of the packet:

| Status bit | Meaning |
|---|---|
| 0 | SYNC was not `0x80` |
| 1 | the PID's upper nibble is not the complement of the lower |
| 2 | CRC wrong (tokens: CRC5, data packets: CRC16; handshakes have none) |
| 3 | unused (0) |
| 4 | the packet was not a whole number of bytes long |
| 5 | bit-stuffing violation (seven 1s in a row): the packet was abandoned |
| 7:6 | PID bits 1:0: 1 token, 3 data, 2 handshake, 0 special |

A good packet has status `0x40` (token), `0xC0` (data) or `0x80` (handshake). The byte count is the host's to check
(a token is 3 bytes, a handshake 1); the firmware does not.

Example: a SETUP token to address 0, endpoint 0 is reported as the bytes `2d 00 10`, then status `40`. The first
data packet of a GET_DESCRIPTOR request, `c3 80 06 00 01 00 00 40 00 dd 94`, is reported byte by byte, then `c0`.

## How it works

**Thread 0 is the line receiver and it never samples.** At every bit boundary it polls D+ with nine `JPIN`
instructions in a row, one per turn, around the place where the next edge is expected. An edge inside the window is
a 0 (NRZI toggles on 0), no edge is a 1. An edge also moves the window: the next one starts exactly 13 turns after the
poll that saw it, so the receiver follows the transmitter's clock at every 0 without a PLL. After a bit without an edge
the next window starts 9 turns after the last poll (17 turns per bit; the real bit is 16.67 turns, so runs of 1s drift
by up to 0.33 turn per bit, and bit stuffing guarantees an edge at least every 7 bits). The bus level, J or K, is the
program counter (one copy of the window for each). The byte under construction is `A`, shifted with `ROR` and finished
by a sentinel bit that falls out into the carry after eight bits; `X` counts down the 1s in a row, which also finds the
stuffed 0s (dropped) and the stuffing violations. SE0 (end of packet) is D- low while D+ is low, looked at in the
middle of a bit. Every byte and every event goes to thread 1 through the mailbox.

**Thread 1 is the protocol.** It is idle in `RECV`. For each byte it reports it on `uo[7:0]` with a BYTE strobe, checks
SYNC (first byte) and the PID (second byte: the byte and its nibble swap must XOR to `0xFF`), and updates the running
CRC of the packet type: CRC5 bit by bit (reflected register, polynomial `0x14`, residue `0x06`), CRC16 with a closed form
per byte instead of eight shifts: with `x = low ^ byte` and `p = parity(x)`, the new low byte is
`high ^ p ^ (x << 6) ^ (x << 7)` and the new high byte `p*0xC0 ^ (x >> 1) ^ (x >> 2)`, checked for all 256 values of `x`
against the bitwise definition, and by the tests against the model's CRC. At the end of the packet (the EVENT flag and a code from thread 0) it compares the
residue (`0xB001` for CRC16) and sends the status byte.

The data memory holds the byte count, the CRC register (two cells), the status bits so far and the packet type.

## What is proved about its timing

The timing analyzer (`tools/timing.py`, `docs/timing-analysis.md`) proves the window schedule for every possible bit
sequence, stuffing, byte boundary and error path (`firmware/timing/usb_ls_rx.timing`, 15,354 abstract states):

| | Proved |
|---|---|
| from the instruction after a poll that saw an edge (J or K) to the first poll of the next window | exactly 12 turns, on every path |
| from the instruction after the last poll without an edge to the first poll of the next window | exactly 8 turns, on every path, also at a byte boundary |
| the nine polls of a window | exactly one turn apart |
| one bit without an edge | exactly 17 turns (680 ns) |

So the receiver is a tracking loop with a known step, and the tolerance below is a consequence of those numbers, not
of luck. Thread 1 (`usb_ls_rx_thread1.timing`, 393,309 states, memory unknown): every message is handled in at most
69 turns (2.76 µs), an event alone in 37, and the report strobes are exactly 4 turns (160 ns).

The mailbox holds one byte, and `SEND` waits if it is full, which would break thread 0's timing. It does not happen:
the analyzer gives thread 0's spacing between messages (byte to next byte at least 101 turns, byte to event at
least 9, event to byte at least 103), which is more than the 69 turns thread 1 needs for a byte, except for the two
messages at the end of a packet, which are 9 turns apart at the closest (a byte that completes on the edge to SE0);
the event then waits in the mailbox for the 69 turns of the byte before it, and the next packet is at least 103 turns
away. Thread 0 can be made to wait only by two end-of-packet events less than 69 turns apart (the analyzer's smallest is 35,
a glitch of K and SE0 straight after an end of packet); a legal packet is at least 19 bits, 316 turns, long.

The same specs are checked against the RTL: `test/test_timing_rtl.py` runs the receiver on the chip against random
packets with jitter and a skewed rate, and every interval (1,195 for thread 0, 222 for thread 1) is inside the proved
range; the window intervals are exactly 12, 8 and 9 turns and the slowest message takes exactly 69 turns.

## Tolerance

Measured on the instruction-set model against the waveform of `test/usb_ls.py`, with the worst-case packets (a byte of
all 1s, which needs the most stuffing; all 0s, which has the most edges; the alternating run lengths) at 17 start phases:
every run is received correctly with the bit time from **2.0 % shorter to 5.0 % longer** than 666.67 ns, outside
that range the runs of 1s drift out of the window. The USB low-speed limit is ±1.5 %. Edge jitter of up to ±4 clocks (80 ns) is tolerated at ±1.5 %, and the
tests also use a skew of 2 clocks between D+ and D- with ±2 clocks of jitter. After the end of a packet the chip listens again
within a few turns, so packets 10 clocks apart (the harness's smallest) are all received. These are simulation results
against the model in this repository, not measurements on a USB host.

## How it is tested

| What | Where |
|---|---|
| the reference model against published check values (CRC16 `0xB4C8`, CRC5 `0x19`, the SETUP token, a GET_DESCRIPTOR request) | `test/test_usb_ls_model.py` (55 tests) |
| the firmware on the instruction-set model: known packets, 60 random packets at the rate limits with jitter and skew, worst-case patterns at every phase, every error kind, recovery, back-to-back packets, three mutations of the firmware | `test/test_usb_ls_rx_model.py` (25 tests) |
| the same on the chip's RTL, and on the gate-level netlist in CI | `test/test_usb_ls_rx_rtl.py` (pins only) |
| the analyzer's ranges against the RTL | `test/test_timing_rtl.py` (`test_usb_ls_rx_thread0_loaded`, `..._thread1_loaded`) |
| the analysis itself | `firmware/timing/usb_ls_rx.timing`, `usb_ls_rx_thread1.timing` |

## Limits

- Receive only. No SOF-based timing, no enumeration, no response to the host (for sending, see the host above).
- The bus model is this repository's own. It was checked against published CRC values and known packets, not against a
  USB host, a cable or an analyser, and no silicon has run this program.
- Electrical USB is not addressed: D+ and D- are taken as logic inputs on `ui[1:0]` and a proper receiver needs the
  external front end (level, the series resistors, the pull-up on the device side) the pads do not provide.
- SE0 is looked at once per bit, about 5 turns after the boundary, so a glitch shorter than that is not an end of packet.
- Bit-time tolerance is asymmetric (−2.0 % to +5.0 %) because the window steps 17 turns while the bit is 16.67.
- The 246 words leave no room for the host's transmitter in the same image.
