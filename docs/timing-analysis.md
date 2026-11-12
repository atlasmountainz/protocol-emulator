# Protocol timing as a proof: static timing analysis of the firmware

`tools/timing.py` takes a program and a list of timing requirements taken from a protocol standard and either
proves that **every possible execution** meets them, or prints the path that breaks one. It does not simulate
and it does not sample: the answer holds for every input, every data value and every behaviour of the other
end of the wire.

It is possible because of the architecture. A thread of this processor runs one instruction per turn of its
clock slot(s), a `WAIT n` takes `1 + n` turns, nothing else takes longer, and no other thread, interrupt or
arbitration can delay it (`formal/pipeline.v` and `formal/isolation.v` prove this about the RTL). The time between
two instructions of a thread is therefore a function of the path it took, and of nothing else. On an ordinary
microcontroller the same question needs a worst-case-execution-time tool and a model of caches, interrupts and
bus contention, and it still gives only a bound. Here it is exact.

## What the tool computes

Inputs are free: every pin is unknown, so every `JPIN` goes both ways. The tool explores all paths with an abstract
interpreter of the instruction set that tracks `A`, `X`, `C`, the return stack and the 16 data bytes as known
numbers where they are known (a `LDX 8`, a loop counter) and as *unknown* where they depend on a pin. From the graph
of reachable states it answers, for two named events:

- the **shortest and longest time** from an occurrence of the first to the next occurrence of the second,
  with the path that attains each;
- the **set of all possible times** when it is finite (a WS2812 bit's high time is exactly `{320 ns, 720 ns}`);
- **"unbounded"** when a loop that waits for an input can run for ever, reported as such and never guessed.

An *event* is a pin write (`out P`, `out P=V`, open-drain `pull P` / `release P`), or the execution of an
instruction (`at LABEL+N`), or an alternative of several (`a | b`). A check compares the range with a number
(`>= 4.7us`, `== 8.64us`, `in [250ns, 550ns] [650ns, 950ns]`), optionally on paths that avoid another event
(`avoid FRAME_END`, to leave out the idle wait between two frames).

An unknown value never makes a result unsound: a write whose value is unknown counts as "may" for minimums and
is ignored for maximums, so the range always contains what the machine can really do.

## Using it

A spec lists the firmware, the thread and schedule (which give the clocks per turn: 4, or 2 for a thread that owns
two slots), the entry point and the requirements. `firmware/timing/i2c_master.timing`:

    program ../main.asm ../uart_tx.asm ../spi_master.asm ../i2c_master.asm ../i2c_monitor.asm ../echo.asm
    start i2c_start_fw
    event SCL_LOW  = pull 2
    event SCL_REL  = release 2
    check SCL_LOW -> SCL_REL >= 4.7us            # tLOW

    python3 tools/timing.py firmware/timing/*.timing        # exit status 1 if any check fails; -v shows the paths

The ROM specs list exactly the files that `tools/build_rom.sh` assembles, in the same order, so the analysed
program *is* the ROM image (a test compares it with `src/rom.v`).

## Results

Every line below is proved for all executions (`python3 tools/timing.py firmware/timing/*.timing`). "exact" means
the minimum equals the maximum.

| Firmware | Requirement | Proved |
|---|---|---|
| ROM UART transmitter | every TX write 8.64 µs (432 clocks) after the previous, for every data byte | exact: 108 turns |
| ROM SPI master (1 MHz) | SCK high ≥ 100 ns, low ≥ 100 ns, data setup ≥ 50 ns, hold ≥ 50 ns, CS setup / hold ≥ 100 ns | SCK high 400 ns, low ≥ 560 ns, MOSI setup ≥ 320 ns, hold 640 to 880 ns, CS setup 800 ns, CS hold ≥ 400 ns; inside a byte the SCK period is exactly 960 ns (12 turns) |
| ROM I2C master, standard mode | tLOW ≥ 4.7 µs, tHIGH ≥ 4.0 µs, tSU;STA ≥ 4.7 µs, tHD;STA ≥ 4.0 µs, tSU;STO ≥ 4.0 µs, tBUF ≥ 4.7 µs, tSU;DAT ≥ 250 ns, SDA hold after SCL falls ≥ 300 ns | 4.96 to 5.36 µs, ≥ 4.8 µs, 4.8 µs, 4.88 µs, 4.8 µs, ≥ 15.6 µs, ≥ 4.88 µs, ≥ 320 ns, on every path, for any slave |
| WS2812 driver | T0H / T1H, T0L / T1L within ±150 ns of the WS2812B datasheet | high `{320, 720}` ns, low `{560, 960}` ns, bit period exactly 1.28 µs, across byte boundaries too |
| MIDI transmitter | bit time 32 µs ±1 % | exact: 400 turns |
| 1-Wire master | reset ≥ 480 µs, presence sampled 60 to 75 µs after release, write-1 low 1..15 µs / write-0 low 60..120 µs, read low 1..15 µs and sampled ≤ 15 µs after the falling edge, every slot 60..120 µs | reset 500.3 µs, sample 70.08 µs, low `{6, 65.12}` µs, read low 6 µs, sample 12.08 µs, slots 69.04 to 70.4 µs |
| Low-speed USB receiver (`docs/usb-low-speed.md`), schedule `0,1,0,1` | the window of nine polls starts exactly 13 turns after the poll that saw an edge and 9 after the last poll without one, for every bit sequence, byte boundary and stuffed bit; thread 1 answers every message within the time between two bytes | 12 turns after an edge, 8 after none, polls one turn apart, 17 turns per bit, on every path; thread 1 at most 69 turns per message, strobes exactly 160 ns |
| 5 MHz SPI master (fast schedule) | the claims in the file header | SCK period 200 ns (5 turns of 40 ns), high 80 ns, low 120 ns, MOSI setup 40 ns, CS_N 3 turns before the first and after the last SCK edge |

## What it found

With the check `SCL_LOW -> SDA_ANY >= 300ns` added to the I2C spec, the analyzer failed the ROM I2C master as it
was: on some path SDA changed **only 160 ns (two turns) after SCL was pulled low**. I2C has no minimum data hold
time in standard mode (tHD;DAT is 0), but the specification asks a device to hold SDA for at least 300 ns after
SCL falls, to bridge the undefined region of the falling edge: a slave that sees SDA move while SCL is still above
its low threshold can mistake the change for a START or a STOP. (That figure is quoted from the I2C specification
from memory and has not been re-read against UM10204 here; see the sources below.) The fix is a `WAIT 1` in four
places of `firmware/i2c_master.asm`: at the start of `i2c_start`, `i2c_stop` and `i2c_rd_byte`, and after the SCL
fall inside the write-bit loop. That is four words more in the ROM (158 to 162). The same check now proves
≥ 320 ns on every path, and the RTL tests of the I2C master still pass, as does the new RTL cross-check, whose
slowest-case reading of 4 turns (320 ns) is attained by the real chip. The other checks held the first time, which
is also a result: the figures quoted in the comments of the firmware (500.3 µs, 70.08 µs, 12.08 µs, the WS2812
windows, ...) are reproduced exactly by a tool that does not read the comments.

## How the analyzer itself is verified

An unsound analyzer is worse than none, so it is tested against the reference model of the instruction set
(`test/isa_model.py`, the model the RTL is differentially tested against) and against the RTL:

1. **Every instruction word.** For all 65,536 words, with four combinations of known `A` and `C`, the pins an
   instruction writes (and the values) are exactly the pins the model writes. Writes that do not change a pin are
   detected with two model copies whose output registers start as complements. With `A` and `C` unknown the same
   pins are written, with a known or an unknown value, never a wrong one.
2. **Abstract step against model step.** 60,000 random instructions in random states, each state blurred into
   "unknown" in random places: the model's next state, the cost in turns and the pin writes are always among those
   the abstract step allows.
3. **Random programs.** Random programs (119 seeds, 103 of them small enough to analyse) run on the model with
   random inputs; every interval between two pin writes that happens lies inside the range the analyzer computed
   for that pair of events (17,812 observed intervals compared).
4. **Can it fail?** Eight deliberately injected bugs in the analyzer itself (the cost of `WAIT`, `DJNZ`, `ADC`'s
   carry, a branch target, the return stack, an event, a pin level, an input branch) are all caught. The shipped
   specs pass, and broken firmware (one `WAIT` too short, a data-dependent extra instruction, the I2C hold fix
   removed) makes them fail.
5. **Hand-computed programs** (counted loop 20 turns, branch `{3, 5}`, spin loop unbounded, a two-slot thread's
   turn being 2 clocks).
6. **Against the RTL** (`test/test_timing_rtl.py`, `make COCOTB_TEST_MODULES=test_timing_rtl`). The real chip
   runs each firmware against hostile or random inputs (a stretching, randomly ACKing and NACKing I2C slave, a
   1-Wire device, random MISO and data, ...) while the testbench records which instruction the thread executes at
   every clock. Every interval between two events in the trace lies in the analyzer's range and in its exact set.
   About 5,100 intervals are compared (UART 180, SPI 896, I2C 256, WS2812 1,364, fast SPI 640, MIDI 72, 1-Wire 210, USB receiver 1,513).
   For every bounded range in these runs both ends are attained by the RTL (I2C tLOW 62 to 67 turns, WS2812 4 to 9
   and 7 to 12 turns, SPI hold 8 to 11, 1-Wire exact, USB window 12 / 8 / 9 turns exact and thread 1 up to 69), so the ranges are tight, not merely safe. With a wrong cost
   for `WAIT` in the analyzer the comparison fails.

The chain is: formal proofs (one instruction per turn, isolation) → RTL equals the model (random differential
test, every register every clock) → the analyzer equals the model (1 to 5) → the analyzer's ranges contain what the
RTL does (6).

## Limits

- One thread at a time. The analysis assumes the thread is never held up by the channel (`nonblocking`); cross-thread
  timing that depends on the partner (the WS2812 latch gap is thread 1's `WAIT`, the SPI turbo example relies on two
  threads running in lock step) is covered by simulation, not by this tool.
- Inputs are unconstrained. A bound that needs an assumption about the other end (a slave that must answer within
  X) is reported unbounded rather than assumed.
- It proves timing of the firmware, not that the firmware implements the protocol; the protocol behaviour is
  checked by the directed and random tests.
- It checks the time between instructions' execution. Pad delays, rise times and the 2-flop input synchroniser
  (a pin change reaches the thread a few clocks late, as the docs say) are outside it.
- State-space: firmware that keeps much known data in registers and memory (a CRC over every byte value) can exceed
  the 3,000,000 abstract states and the tool stops with an error instead of answering. Data memory starts
  unknown by default (`mem zero` is the opt-in), which is what keeps the shipped specs between 39 and 393,309 states.
- The protocol limits in the specs come from: the WS2812B datasheet (T0H 0.4 µs, T1H 0.8 µs, T0L 0.85 µs,
  T1L 0.45 µs, each ±150 ns; checked against the datasheet text), standard-mode I2C (the
  minimums 4.7 / 4.0 / 4.7 / 4.0 / 4.0 / 4.7 µs and 250 ns were checked against a vendor datasheet's copy of the
  standard-mode table, not against UM10204 itself; the 300 ns SDA hold is quoted from memory of the specification
  and was not re-read), Maxim 1-Wire standard-speed timing
  (480 µs reset, 6 / 60 / 70 µs recommended values checked in Analog Devices' article; the allowed windows for
  presence, write-0 and the read sample come from the DS18B20-class device datasheets, quoted from memory), MIDI 1.0
  (31.25 kbaud ±1 %) and, for SPI, representative values of a 3.3 V EEPROM / flash class part rather than one
  data sheet. Anyone relying on a figure should compare it with the standard for their own device.
