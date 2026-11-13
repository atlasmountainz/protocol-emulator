# How the design is verified

This is a factual list of what was run and what each check can and cannot catch. Numbers are from runs of
the committed code; `docs/results.md` has the hardware results.

## 1. Directed protocol tests (cocotb + Icarus Verilog)

`test/test.py` holds 17 tests, run three ways: from the ROM, from the loadable program memory
(`FROM_RAM=1`, the whole firmware loaded over the pins), and on the synthesized gate-level netlist in CI.

- **UART transmitter:** frame format and a cycle-exact test that the gap between two bytes is the expected
  number of clocks.
- **SPI master:** bit order, clock polarity and data in both directions.
- **I2C master** against a bus model with timing checks (START/STOP setup and hold, SCL low and high
  times, ACK/NACK, clock stretching): EEPROM read, NACK handling, a stretching slave.
- **Full-duplex UART echo** with idle gaps and with a sender 0.4 % fast (two threads, one channel).
- **I2C SCL monitor** (a second thread watching SCL while the master runs): quiet on good traffic, silent on a
  stretching slave, flags a rogue slave that clips the clock, and exact thresholds on the low and high phase
  lengths.
- **Program loader:** custom program, ignored data without a strobe, reset returns to the ROM, partial load
  followed by a reload.

The loadable examples (programs the host loads over the pins; see `docs/examples-*.md`) have their own test
modules, each loading the program through the real pin loader and driving it with a protocol model that checks
the timing windows:

| Module | Tests | Covers |
|---|---|---|
| `test_leds_keyboard_midi.py` | 7 | WS2812 waveform and pixel decode against the spec windows; PS/2 receive at 10 and 16.7 kHz with jitter and error frames; MIDI byte timing |
| `test_i2c_target.py` | 18 | I2C target (slave) at 100 and 400 kHz: ACK/NACK, repeated start, auto-increment, wrong address, back-to-back transactions; margin test at 588 kHz |
| `test_spi_target.py` | 10 | SPI target (slave) in modes 0 and 3: ID, table read with auto-increment, status counter, CS re-framing at any bit, clock duty cycle, minimum CS timing, 60 random frames per mode; rates from 250 kHz to 1.35 MHz |
| `test_onewire_jtag.py` | 10 | 1-Wire master with in-firmware CRC8 (good, corrupted and missing devices, stuck bus); JTAG master against a TAP model (1 to 5 devices in the chain, dead TDO); the chip must drive no pad other than the protocol's own |
| `test_i2c_target_regs.py` | 28 | I2C target with 16 registers in the data memory: sweeps, single reads, wraparound, repeated start, wrong address; 100 and 400 kHz on two schedules; SCL sweep to the limit; 15 firmware mutants |
| `test_spi_master_fast.py` | 13 | SPI master at 5 MHz (schedule `0,1,0,2`) and 8.33 MHz (`0,1,0,1`): SCK period and duty from edge timestamps, MOSI setup and hold, CS_N timing, MISO sampling window; 17 firmware mutants |
| `test_usb_ls_rx_rtl.py` | 3 | the low-speed USB receiver (`docs/usb-low-speed.md`) on pins only: known packets, ±1.5 % bit rate with jitter and skew, wrong CRCs / PID / stuffing violation and recovery; the same scenarios run on the instruction-set model in `test_usb_ls_rx_model.py` (pure Python, 25 tests) against the bus model `test/usb_ls.py` (`test_usb_ls_model.py`, 55 tests, published CRC check values) |
| `test_swd_can_rtl.py` | 4 | the SWD host and the CAN receiver (`docs/examples-swd-can.md`) on pins only: the SWD power-up sequence against a debug-port model with WAIT answers and a wrong parity bit, nobody there, FAULT; CAN frames at 1 % off with jitter received and acknowledged in the ACK slot, a wrong CRC and an error flag not acknowledged, listen-only. The same and many more scenarios run on the instruction-set model in `test_swd_model.py` (34 tests, 8 firmware mutants) and `test_can_model.py` (42 tests, 10 firmware mutants), pure Python |
| `test_isa_v2_rtl.py` | 18 | the ISA v2 instructions on the real chip: ALU with carry and borrow corner values, every branch condition taken and not taken, data memory (direct, X-indexed, wraparound, per thread), `JMPA` / `CALLA` / `RETL` lookup tables, wide pin access, undefined sub-opcodes as `NOP`, every kind of thread schedule (default, `0,1,0,2`, `0,1,0,1`, ... , an illegal one, none sent) |

## 2. Random programs against an independent model

`test/isa_model.py` is a second implementation of the instruction set, written from `docs/isa.md`. It shares
only the assembler with the RTL. `test/random_run.py` generates random programs (every mnemonic, random branch
targets, random pin stimulus, four threads, channel and SPAWN traffic), runs each on the RTL and on the model,
and compares every architectural register of all four threads on every clock edge. It also fails if any
instruction or branch outcome was never exercised across the seeds.

The model and the generator cover instruction set version 2 (about one program in five is version 1 only) and, in
the loaded-program runs, a random thread schedule: mostly legal ones including the fast ones, and about one in
eight deliberately illegal, where the chip must fall back to the default. The comparison includes all 64 bytes
of data memory. 120 coverage counters (every instruction, both outcomes of every branch, carry and borrow set and
clear, memory wraparound, ...) must all be hit across the seeds, otherwise the run fails.

Latest runs on the final design: 100 of 100 seeds match with the program in the ROM, and 100 of 100 match with
each program loaded into the program memory over the pins with its own schedule. A random program is at most 256
instructions. The model itself has 478 pure-Python tests with hand-derived expected values; 17 deliberate
mutations of the model (borrow polarity, rotate carry, memory wraparound, fetch slot, memory sharing, ...) were
each caught.

## 3. Mutation checks

A test suite that cannot fail proves nothing, so each firmware test was checked by deliberately breaking the
firmware (a wait count one too short, a flipped bit order, a removed stop-bit check, a wrong polarity) and
confirming that a test fails. Every mutation of the protocol firmware was caught. The first mutation of the echo
firmware that survived turned out not to be a bug (the changed wait fell inside the same bit), and a real
mutation of the same line was caught. `docs/examples-leds-keyboard-midi.md` lists the 15 mutations of the
WS2812, PS/2 and MIDI programs; the rewritten 1-Wire and JTAG programs were mutated 21 times (CRC polynomial, carry
polarity, bit order, status polarity, counters, wait counts, a scratch-pad access, ...), the register-file I2C target
15 times and the fast SPI masters 17 times, and every mutant was caught.

## 4. Formal proofs (`formal/`, Yosys + z3)

Run with `formal/run.sh`. The harnesses wrap the real `src/cpu.v`; the only change is a set of observation
ports compiled in under `ifdef FORMAL`, so the chip's RTL is unchanged.

- **Pipeline invariants** (`formal/pipeline.v`), proved inductively for every state reachable from reset, with
  a reset allowed at any moment and for **every legal thread schedule** (the schedule table is an arbitrary
  constant, so the proof covers the default schedule and all the faster ones, including the program-counter
  forwarding path that a thread owning two slots needs): the instruction being executed belongs to the thread that the schedule fetched in the
  previous clock; the fetch address is always that thread's program counter; the instruction register holds the
  word at that program counter; the slot counter advances by exactly one every clock.
- **Thread isolation** (`formal/isolation.v`), for each of the four threads: two copies of the CPU receive the
  same pins and have the same program memory, and thread T starts in the same state in both, but the other
  three threads start in arbitrary, different states. The proof is that thread T then stays identical in both
  copies forever, including the clock cycle in which each of its instructions executes. The two excluded
  interaction points are the channel (thread T does not execute SEND or RECV) and SPAWN (no other thread
  spawns T); everything else the other threads can do is covered. Thread T's 16 bytes of data memory are
  part of the compared state, and the schedule is an arbitrary legal table shared by both copies. Result: proved for
  all four threads (base case and induction step both pass; the relation is preserved by every clock step, so
  the induction depth is 2).
- **The proofs can fail:** `formal/run.sh mutant` runs them against three deliberately broken CPUs: (1) a thread's
  shift result depends on the next thread's program counter, (2) a memory load reads the next thread's data memory
  (both against the isolation proof, all four threads), (3) the program-counter forwarding path is removed (against
  the pipeline invariants). All nine runs report a counterexample. Before this check existed the isolation proof
  was found to be vacuous (the program memory had been folded into a constant ROM), which is why the check is
  kept.

## 5. Static timing analysis of the firmware

`tools/timing.py` proves protocol timing for every execution instead of sampling it: it explores all paths of a
firmware (every pin input free, every data value unknown unless the program fixed it) and gives the exact minimum,
maximum and set of times between two events, or reports a loop that depends on an input as unbounded. It is
possible because a thread's time between two instructions depends only on the path it took (section 4, the
pipeline and isolation proofs). The method, the specs (`firmware/timing/*.timing`), the results and the limits are
in `docs/timing-analysis.md`. The analyzer is itself checked in six ways:

| Module | Tests | Covers |
|---|---|---|
| `test/test_timing.py` (pure Python, about 20 s) | 33 | every one of the 65,536 instruction words: the pins written equal the model's; 60,000 random abstract steps against the model's step; 119 random programs (103 analysable) with 17,812 observed intervals inside the analysed ranges; the shipped specs hold; broken firmware (a short `WAIT`, a data-dependent extra instruction, the I2C hold delay removed) fails them; 8 deliberate bugs in the analyzer were each caught; hand-computed programs; the `forget` directive only ever widens a range (random programs); `src/rom.v` equals the assembled firmware (162 words) and the ROM specs analyse that program |
| `test/test_timing_rtl.py` (RTL only: `make COCOTB_TEST_MODULES=test_timing_rtl`) | 13 | the real RTL runs each firmware against a hostile I2C slave, a 1-Wire device, random USB and CAN traffic, an SWD debug-port model and random data while every executed instruction is traced; all 8,905 intervals between the specs' events lie inside the analysed ranges and sets, and both ends of every bounded range are reached; a deliberately wrong cost model is caught |

It found one real defect: the ROM I2C master could change SDA 160 ns after SCL fell. Four `WAIT 1` were added
(158 to 162 ROM words) and the check now proves at least 320 ns on every path.

## 6. Hardware flow checks (CI)

Every push runs the Tiny Tapeout flow: synthesis, place and route at a 20 ns clock, timing in three corners,
antenna, LVS and DRC, the precheck, and the cocotb suites `test.py` (17 tests of the ROM firmware),
`test_isa_v2_rtl.py` (18 tests of the version-2 instructions and the thread schedules) and `test_spi_master_fast.py`
(13 tests of the 5 MHz SPI master), `test_usb_ls_rx_rtl.py` (3 tests of the USB receiver) and `test_swd_can_rtl.py`
(4 tests of the SWD host and the CAN receiver) on the gate-level netlist.
A separate job runs the pure-Python tests (`python -m pytest test`, which includes the instruction-set model, the timing analyzer and the USB,
SWD and CAN programs on the model) and the RTL timing cross-check (`test_timing_rtl.py`); that module reads internal signals of the
CPU, so it is not part of the gate-level run.

## What this does not cover

- The timing analysis covers one thread at a time, with the channel assumed never to block, and it is about
  instruction timing inside the chip, not pads, rise times or the input synchroniser. Cross-thread timing
  (the WS2812 latch gap, the lock-step turbo SPI) is covered by simulation only. The limits it checks against come
  from the standards and datasheets listed in `docs/timing-analysis.md`, several quoted from memory; they should be
  compared with the document for the device actually used.
- No analog or pad-level simulation; the I/O is checked only at the digital boundary.
- The protocol models in the testbenches are written by the same author as the firmware. They are checked
  against the published timing parameters, but not against a physical device.
- Isolation is proved with the channel and SPAWN excluded for the thread under test. Threads that do use the
  channel interact through it by design; that behaviour is covered by the directed and random tests instead.
