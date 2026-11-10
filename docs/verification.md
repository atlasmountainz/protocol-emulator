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
| `test_onewire_jtag.py` | 10 | 1-Wire master with in-firmware CRC8 (good, corrupted and missing devices, stuck bus); JTAG master against a TAP model (1 to 5 devices in the chain, dead TDO) |

## 2. Random programs against an independent model

`test/isa_model.py` is a second implementation of the instruction set, written from `docs/isa.md`. It shares
only the assembler with the RTL. `test/random_run.py` generates random programs (every mnemonic, random branch
targets, random pin stimulus, four threads, channel and SPAWN traffic), runs each on the RTL and on the model,
and compares every architectural register of all four threads on every clock edge. It also fails if any
instruction or branch outcome was never exercised across the seeds.

Latest runs on the 256-word design: 100 of 100 seeds match with the program in the ROM, and 100 of 100 match
with each program loaded into the program memory over the pins. A random program is at most 256 instructions.

## 3. Mutation checks

A test suite that cannot fail proves nothing, so each firmware test was checked by deliberately breaking the
firmware (a wait count one too short, a flipped bit order, a removed stop-bit check, a wrong polarity) and
confirming that a test fails. Every mutation of the protocol firmware was caught. The first mutation of the echo
firmware that survived turned out not to be a bug (the changed wait fell inside the same bit), and a real
mutation of the same line was caught. `docs/examples-leds-keyboard-midi.md` lists the 15 mutations of the
WS2812, PS/2 and MIDI programs.

## 4. Formal proofs (`formal/`, Yosys + z3)

Run with `formal/run.sh`. The harnesses wrap the real `src/cpu.v`; the only change is a set of observation
ports compiled in under `ifdef FORMAL`, so the chip's RTL is unchanged.

- **Pipeline invariants** (`formal/pipeline.v`), proved inductively for every state reachable from reset, with
  a reset allowed at any moment: the instruction being executed belongs to the thread that was fetched in the
  previous clock; the fetch address is always that thread's program counter; the instruction register holds the
  word at that program counter; the slot counter advances by exactly one every clock.
- **Thread isolation** (`formal/isolation.v`), for each of the four threads: two copies of the CPU receive the
  same pins and have the same program memory, and thread T starts in the same state in both, but the other
  three threads start in arbitrary, different states. The proof is that thread T then stays identical in both
  copies forever, including the clock cycle in which each of its instructions executes. The two excluded
  interaction points are the channel (thread T does not execute SEND or RECV) and SPAWN (no other thread
  spawns T); everything else the other threads can do is covered. ISOLATION_RESULT
- **The proof can fail:** `formal/run.sh mutant` runs the same proof against a CPU deliberately changed so that
  a thread's shift result depends on the next thread's program counter. All four runs report a counterexample.
  Before this check existed the proof was found to be vacuous (the program memory had been folded into a
  constant ROM), which is why the check is kept.

## 5. Hardware flow checks (CI)

Every push runs the Tiny Tapeout flow: synthesis, place and route at a 20 ns clock, timing in three corners,
antenna, LVS and DRC, the precheck, and the full cocotb suite on the gate-level netlist.

## What this does not cover

- No analog or pad-level simulation; the I/O is checked only at the digital boundary.
- The protocol models in the testbenches are written by the same author as the firmware. They are checked
  against the published timing parameters, but not against a physical device.
- Isolation is proved with the channel and SPAWN excluded for the thread under test. Threads that do use the
  channel interact through it by design; that behaviour is covered by the directed and random tests instead.
