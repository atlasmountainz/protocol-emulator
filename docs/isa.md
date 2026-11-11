# Architecture and instruction set

The core is a **deterministic four-thread barrel processor** (the idea is the one from the CDC 6600's
peripheral processors, scaled down to a few thousand gates). Four hardware threads share one program memory and
one set of pins and take turns in a fixed, repeating **schedule**: by default thread 0, 1, 2, 3, 0, 1, ... Each
thread executes **one instruction per turn**. Because the schedule never changes while the program runs, a
thread's timing is exact and does not depend on what the other threads are doing. There are no interrupts, no
priorities and no arbitration, and no instruction can make another thread late.

For protocol work that is the point. One thread can be a bit-banged UART transmitter whose bit edges land
on exact clock cycles while another thread, at the same time, is receiving, or checking the timing of a bus.

## Timing and the schedule

- The schedule has **four slots**, one per clock, repeating. Each slot names the thread that gets that clock
  (a clock is one fetch and, one clock later, one execute). The default schedule is `0 1 2 3`: every thread
  gets one turn in four clocks, **one turn = 4 clocks = 80 ns at 50 MHz**.
- A host can load a different schedule together with the program (`docs/loading.md`, byte 512). The only rule is
  that a thread may not own two slots that follow each other (slot 3 and slot 0 follow each other too).
  So a thread owns at most two slots, two clocks apart, and runs at **one instruction every 2 clocks = 40 ns**:

  | Schedule | Thread 0 | Thread 1 | Thread 2 | Thread 3 |
  |---|---|---|---|---|
  | `0 1 2 3` (default) | 80 ns | 80 ns | 80 ns | 80 ns |
  | `0 1 0 2` | 40 ns | 80 ns | 80 ns | not scheduled |
  | `0 1 0 1` | 40 ns | 40 ns | not scheduled | not scheduled |

  A thread that is not in the schedule never runs (a `SPAWN` of it marks it running but it never gets a clock).
  A schedule that breaks the rule is ignored by the hardware and the default is used instead.
- An instruction takes **one turn**, except `WAIT n`, which takes `1 + n` turns. Taken branches, `CALL` and `RET`
  cost nothing extra. There are no stalls and no flushes.
- A thread executes on the same clocks of every four, always. At 50 MHz with the default schedule one turn is 80 ns.
  A UART bit at 115200 baud is 108 turns (432 clocks, 0.5 % fast).
- A pin change reaches the threads through a 2-flop synchroniser, so a thread sees it a few clocks after it
  happens (at its next turn after the 2-clock synchroniser delay).
- Everything below that is written in "turns" (the old documents say "slots", which is the same thing with the
  default schedule).

## State

Per thread: `PC` (8 bit), `A` (8-bit accumulator), `X` (8-bit index / counter), `C` (carry or borrow flag), a
stall counter for `WAIT`, a **4-entry circular return stack** and **16 bytes of private data memory**.
Abandoning a call with a `JMP` instead of `RET` is safe. The data memory is cleared by reset (and so by every
program load) and is not touched by `SPAWN` or `HALT`; no other thread can read or write it.

Shared by all threads:

- **Output pins** `uo[7:0]` (push-pull), reset value `0x01` (so a UART TX line idles high).
  `PIN`, `OUTC` change one bit and `OUT`, `OUTA` replace all eight. Only one instruction runs per clock, so
  two threads never race on the pins. Note that `OUT` overwrites bits another thread may be driving.
- **Open-drain pins** `0-7` = `uio[7:0]`. They are never driven high: `ODRV`, `ODRVC` and `ODRVA` pull a line low
  or release it and the external pull-up does the rest. The pad can always be read back as input pins `8-15`, so
  a slave holding a line low (I2C clock stretching) is visible to the firmware.
- **Input pins**: `0-7` = `ui[7:0]`, `8-15` = `uio[7:0]`, through a 2-flop synchroniser.
- **The channel**: a one-byte mailbox with blocking `SEND` / `RECV`.

At reset only thread 0 runs, at address 0. The other three are stopped until a thread starts them with `SPAWN`.

## Threads and the channel

| Instruction | Effect |
|---|---|
| `SPAWN t addr` | Start thread `t` (0..3) at `addr` with `A = X = C = 0`, an empty return stack and no pending `WAIT`. If `t` is already running and is not the executing thread, the instruction does nothing. `SPAWN` of the executing thread's own number restarts it at `addr`. A stopped thread (after `HALT`) can be started again. The data memory is kept. |
| `HALT` | Stop **this** thread until another thread spawns it again (or reset). |
| `SEND` | `channel = A`. If the channel already holds a byte, the thread waits (retrying every turn) until it is empty. |
| `RECV` | `A = channel`, and the channel becomes empty. If it is empty, the thread waits until a byte arrives. |

A blocked thread simply stays on its `SEND` or `RECV`; the other threads are unaffected. The
channel holds one byte, so when a sender and a receiver run at different speeds the faster one waits.
Programs that rely on that (see `firmware/echo.asm`) get synchronisation for free.

## Instructions

16-bit instructions, 8-bit program counter (256 words). The ROM has 256 words and so has the loadable
program memory (see `docs/loading.md`). Unused ROM addresses read as `HALT`.

The first version of the instruction set had no arithmetic, no data memory and no branch on data. Version 2
adds all of those **without changing the encoding of any version 1 instruction**: every new instruction sits in
bits that version 1 ignored, so programs and ROM firmware written for version 1 run unchanged. Undefined
sub-opcodes are `NOP`.

### Pins, timing and calls (unchanged from version 1)

| Op  | Mnemonic        | Encoding                       | Effect |
|-----|-----------------|--------------------------------|--------|
| 0x1 | `PIN p v`       | `0001 0ppp ---- ---v`          | `OUT[p] = v` |
| 0x2 | `OUT imm8`      | `0010 ---- iiii iiii`          | `OUT = imm8` |
| 0x3 | `WAIT n`        | `0011 nnnn nnnn nnnn`          | take `1 + n` turns in all (n = 0..4095) |
| 0x5 | `JPIN p 1 addr` | `0101 A pppp aaaaaaa`          | if `IN[p] == 1` then `PC = addr` (p = 0..15) |
| 0x0 | `JPIN p 0 addr` | `0000 A pppp aaaaaaa`          | if `IN[p] == 0` then `PC = addr` |
| 0x8 | `OUTC p`        | `1000 0ppp ---- ----`          | `OUT[p] = C` |
| 0xA | `DJNZ addr`     | `1010 ---- aaaa aaaa`          | `X = X - 1; if X != 0 then PC = addr` |
| 0xD | `INB p`         | `1101 -ppp p--- ----`          | `A[0] = IN[p]` (p = 0..15) |
| 0xE | `CALL addr`     | `1110 ---- aaaa aaaa`          | push `PC+1` on this thread's return stack, `PC = addr` |
| 0xF | `HALT`          | `1111 0000 ---- ----`          | stop this thread |
| 0xF | `RET`           | `1111 0001 ---- ----`          | pop this thread's return stack into `PC` |
| 0xF | `ODRV p v`      | `1111 0010 ---- vppp`          | open-drain pin `p`: `v=0` pulls it low, `v=1` releases it (p = 0..7) |
| 0xF | `ODRVC p`       | `1111 0011 ---- -ppp`          | open-drain pin `p`: pulls low if `C=0`, releases if `C=1` |
| 0xF | `NOP`           | `1111 0100 ---- ----`          | nothing |
| 0xF | `SEND`          | `1111 0101 ---- ----`          | channel = `A` (waits while full) |
| 0xF | `RECV`          | `1111 0110 ---- ----`          | `A` = channel (waits while empty) |
| 0xF | `SPAWN t addr`  | `1111 10tt aaaa aaaa`          | start thread `t` at `addr` (see above) |

### Branches (`0100 cccc aaaa aaaa`)

Version 1 had only `JMP` here and ignored `cccc`. The condition field `cccc` now selects:

| cccc | Mnemonic | Branch to `addr` if |
|---|---|---|
| 0 | `JMP addr` | always |
| 1 | `JC addr` | `C == 1` |
| 2 | `JNC addr` | `C == 0` |
| 3 | `JZ addr` | `A == 0` |
| 4 | `JNZ addr` | `A != 0` |
| 5 | `JMI addr` | `A[7] == 1` |
| 6 | `JPL addr` | `A[7] == 0` |
| 7 | `JEQ addr` | `A == X` |
| 8 | `JNE addr` | `A != X` |
| 9..15 | (undefined) | never (`NOP`) |

No branch changes any register. A branch not taken continues at `PC + 1`.

### Accumulator with an immediate (`0110 oooo iiii iiii`)

Version 1 had only `LDA` here (and ignored `oooo`). All arithmetic is unsigned, 8 bit, wrapping.

| oooo | Mnemonic | Effect |
|---|---|---|
| 0 | `LDA imm8` | `A = imm8` (flags unchanged) |
| 1 | `ADD imm8` | `{C, A} = A + imm8` (C = carry out) |
| 2 | `SUB imm8` | `A = A - imm8`; `C = 1` if `A < imm8` before (borrow) |
| 3 | `AND imm8` | `A = A & imm8` (C unchanged) |
| 4 | `OR imm8`  | `A = A \| imm8` (C unchanged) |
| 5 | `XOR imm8` | `A = A ^ imm8` (C unchanged) |
| 6 | `CMP imm8` | `C = 1` if `A < imm8`; `A` unchanged |
| 7 | `ADC imm8` | `{C, A} = A + imm8 + C` |
| 8 | `SBC imm8` | `A = A - imm8 - C`; `C = 1` if `A < imm8 + C` before (borrow) |
| 9..15 | (undefined) | `NOP` |

### Accumulator and X (`0111 ffff ---- ----`)

Version 1 had only `SHR` here (and ignored `ffff`).

| ffff | Mnemonic | Effect |
|---|---|---|
| 0 | `SHR` | `C = A[0]; A = A >> 1` (zero fill) |
| 1 | `ROR` | `A = {C, A[7:1]}; C = old A[0]` |
| 2 | `ROL` | `A = {A[6:0], C}; C = old A[7]` |
| 3 | `INC` | `A = A + 1` (C unchanged) |
| 4 | `DEC` | `A = A - 1` (C unchanged) |
| 5 | `NOT` | `A = ~A` (C unchanged) |
| 6 | `CMPX` | `C = 1` if `A < X`; nothing else changes |
| 7 | `SWAP` | `A = {A[3:0], A[7:4]}` (C unchanged) |
| 8 | `TAX` | `X = A` |
| 9 | `TXA` | `A = X` |
| 10 | `XCHG` | exchange `A` and `X` |
| 11 | `ADDX` | `{C, A} = A + X` |
| 12 | `SUBX` | `A = A - X`; `C = 1` if `A < X` before |
| 13 | `ANDX` | `A = A & X` |
| 14 | `ORX` | `A = A \| X` |
| 15 | `XORX` | `A = A ^ X` |

### X with an immediate (`1001 oooo iiii iiii`)

| oooo | Mnemonic | Effect |
|---|---|---|
| 0 | `LDX imm8` | `X = imm8` (`LDX 0` followed by `DJNZ` loops 256 times) |
| 1 | `XADD imm8` | `X = X + imm8` (no flag) |
| 2 | `XCMP imm8` | `C = 1` if `X < imm8` |
| 3..15 | (undefined) | `NOP` |

### Wide pin access (`1011 ffff ---- ----`)

| ffff | Mnemonic | Effect |
|---|---|---|
| 0 | `IN` | `A = IN[7:0]` (the `ui` pins) |
| 1 | `INH` | `A = IN[15:8]` (the `uio` pads read back) |
| 2 | `OUTA` | `OUT = A` (all eight push-pull outputs) |
| 3 | `ODRVA` | open-drain pad `i` is pulled low if `A[i] == 0` and released if `A[i] == 1`, for all eight pads |
| 4..15 | (undefined) | `NOP` |

### Shifts

| Op  | Mnemonic | Encoding | Effect |
|---|---|---|---|
| 0xC | `SHL` | `1100 ---- ---- ----` | `C = A[7]; A = A << 1` (zero fill); the other `ffff` values are `NOP` |

### Data memory and computed jumps (extended family)

Each thread has 16 private bytes. `LDM` / `STM` address them directly with `off` (0..15); `LDMX` / `STMX` add `X`
first: the address is `(X + off) mod 16`.

| Mnemonic | Encoding | Effect |
|---|---|---|
| `LDM off` | `1111 1100 0 0 -- oooo` | `A = MEM[off]` |
| `LDMX off` | `1111 1100 0 1 -- oooo` | `A = MEM[(X + off) & 15]` |
| `STM off` | `1111 1100 1 0 -- oooo` | `MEM[off] = A` |
| `STMX off` | `1111 1100 1 1 -- oooo` | `MEM[(X + off) & 15] = A` |
| `JMPA` | `1111 1101 ---- ---0` | `PC = A` (computed jump; with `ADD` a jump table) |
| `CALLA` | `1111 1101 ---- ---1` | push `PC+1`, `PC = A` |
| `RETL imm8` | `1111 0111 iiii iiii` | `A = imm8`, then pop the return stack into `PC` (a table of `RETL` gives a lookup) |

The remaining extended sub-opcodes (`0xE`, `0xF`) are `NOP`.

Notes:
- `JPIN` encodes its pin state in the opcode (`0x5` = jump if high, `0x0` = jump if low), so the freed bit 11
  becomes address bit 7. Because `0x0000` is a `JPIN p 0` instruction, unused ROM words are filled with
  `HALT` (`0xF000`), not zeros.
- Programs are written in assembly (`firmware/*.asm`) and turned into `src/rom.v` by
  `tools/build_rom.sh`, which assembles `firmware/main.asm` (boot dispatcher), `uart_tx.asm`,
  `spi_master.asm`, `i2c_master.asm`, `i2c_monitor.asm` and `echo.asm` into one ROM (158 words).
  Labels are global across files.
- A lookup table without data memory: `ADD base; CALLA` into a run of `RETL v0`, `RETL v1`, ... returns
  `A = v[index]`.

## How the hardware does it

Two pipeline stages: in one clock the instruction of the thread whose slot it is gets fetched; in the next it
executes. With the default schedule consecutive instructions belong to different threads and a thread's next
fetch is three clocks after its execute, so it always sees its own updated `PC`, there are no data hazards, and no
forwarding. With a faster schedule a thread owns two slots two clocks apart: its next fetch is then the clock
right after its execute starts, so the new `PC` is forwarded straight from the execute stage into the fetch
address (the same path that `SPAWN` uses). Nothing else is forwarded: `A`, `X`, `C` and the memory are only
read in the execute stage, and the thread's previous instruction has finished by then. Either way there is no
branch penalty and the machine runs at the full clock.

## Verification

The instruction set above is also written as a separate reference model (`test/isa_model.py`) that knows nothing about the
pipeline. `test/random_test.py` runs random programs (every instruction, random branch targets, `SPAWN` and the channel
included, random schedules) on the model and on the RTL and compares every architectural register of every thread after
every clock edge. `docs/verification.md` has the rest.
