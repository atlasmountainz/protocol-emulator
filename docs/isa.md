# Architecture and instruction set

The core is a **deterministic four-thread barrel processor** (the idea is the one from the CDC 6600's
peripheral processors, scaled down to a few thousand gates). Four hardware threads share one program memory and
one set of pins and take turns in a fixed order: thread 0, 1, 2, 3, 0, 1, ... Each thread executes **one
instruction every fourth clock** (one *slot*). Because the order never changes, a thread's timing is exact
and does not depend on what the other threads are doing. There are no interrupts, no priorities and no
arbitration, and no instruction can make another thread late.

For protocol work that is the point. One thread can be a bit-banged UART transmitter whose bit edges land
on exact clock cycles while another thread, at the same time, is receiving, or checking the timing of a bus.

## Timing

- A thread's instruction takes **1 slot = 4 clocks**; `WAIT n` takes `1 + n` slots.
- Taken branches, `CALL` and `RET` cost nothing extra. There are no stalls and no flushes.
- At 50 MHz one slot is 80 ns. A UART bit at 115200 baud is 108 slots (432 clocks, 0.5 % fast).
- A thread executes on every fourth clock, always the same ones. A pin change reaches the threads through a
  2-flop synchroniser, so a thread sees it a few clocks after it happens (at its next slot after the
  2-clock synchroniser delay).

## State

Per thread: `PC` (8 bit), `A` (8-bit accumulator / shift register), `X` (8-bit loop counter),
`C` (carry), a stall counter for `WAIT`, and a **4-entry circular return stack**. Abandoning a call with
a `JMP` instead of `RET` is safe.

Shared by all threads:

- **Output pins** `uo[7:0]` (push-pull), reset value `0x01` (so a UART TX line idles high).
  `PIN`, `OUTC` change one bit and `OUT` replaces all eight. Only one instruction runs per clock, so
  two threads never race on the pins. Note that `OUT` overwrites bits another thread may be driving.
- **Open-drain pins** `0-7` = `uio[7:0]`. They are never driven high: `ODRV` pulls a line low or
  releases it and the external pull-up does the rest. The pad can always be read back as input
  pins `8-15`, so a slave holding a line low (I2C clock stretching) is visible to the firmware.
- **Input pins**: `0-7` = `ui[7:0]`, `8-15` = `uio[7:0]`, through a 2-flop synchroniser.
- **The channel**: a one-byte mailbox with blocking `SEND` / `RECV`.

At reset only thread 0 runs, at address 0. The other three are stopped until a thread starts them with `SPAWN`.

## Threads and the channel

| Instruction | Effect |
|---|---|
| `SPAWN t addr` | Start thread `t` (0..3) at `addr` with `A = X = C = 0`, an empty return stack and no pending `WAIT`. If `t` is already running and is not the executing thread, the instruction does nothing. `SPAWN` of the executing thread's own number restarts it at `addr`. A stopped thread (after `HALT`) can be started again. |
| `HALT` | Stop **this** thread until another thread spawns it again (or reset). |
| `SEND` | `channel = A`. If the channel already holds a byte, the thread waits (retrying every slot) until it is empty. |
| `RECV` | `A = channel`, and the channel becomes empty. If it is empty, the thread waits until a byte arrives. |

A blocked thread simply stays on its `SEND` or `RECV`; the other threads are unaffected. The
channel holds one byte, so when a sender and a receiver run at different speeds the faster one waits.
Programs that rely on that (see `firmware/echo.asm`) get synchronisation for free.

## Instructions

16-bit instructions, 8-bit program counter (256 words). The ROM has 256 words and so has the loadable
program memory (see `docs/loading.md`). Unused ROM addresses read as `HALT`.

| Op  | Mnemonic        | Encoding                       | Effect |
|-----|-----------------|--------------------------------|--------|
| 0x1 | `PIN p v`       | `0001 0ppp ---- ---v`          | `OUT[p] = v` |
| 0x2 | `OUT imm8`      | `0010 ---- iiii iiii`          | `OUT = imm8` |
| 0x3 | `WAIT n`        | `0011 nnnn nnnn nnnn`          | take `1 + n` slots in all (n = 0..4095) |
| 0x4 | `JMP addr`      | `0100 ---- aaaa aaaa`          | `PC = addr` |
| 0x5 | `JPIN p 1 addr` | `0101 A pppp aaaaaaa`          | if `IN[p] == 1` then `PC = addr` (p = 0..15) |
| 0x0 | `JPIN p 0 addr` | `0000 A pppp aaaaaaa`          | if `IN[p] == 0` then `PC = addr` |
| 0x6 | `LDA imm8`      | `0110 ---- iiii iiii`          | `A = imm8` |
| 0x7 | `SHR`           | `0111 ---- ---- ----`          | `C = A[0]; A = A >> 1` |
| 0x8 | `OUTC p`        | `1000 0ppp ---- ----`          | `OUT[p] = C` |
| 0x9 | `LDX imm8`      | `1001 ---- iiii iiii`          | `X = imm8` |
| 0xA | `DJNZ addr`     | `1010 ---- aaaa aaaa`          | `X = X - 1; if X != 0 then PC = addr` |
| 0xB | `IN`            | `1011 ---- ---- ----`          | `A = IN[7:0]` |
| 0xC | `SHL`           | `1100 ---- ---- ----`          | `C = A[7]; A = A << 1` |
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

Notes:
- `LDX 0` loops 256 times (X wraps to 255 before the first test).
- Extended sub-opcodes that are not listed (`0x7`, `0xC` to `0xF`) behave as `NOP`.
- `JPIN` encodes its pin state in the opcode (`0x5` = jump if high, `0x0` = jump if low), so the freed bit 11
  becomes address bit 7. Because `0x0000` is a `JPIN p 0` instruction, unused ROM words are filled with
  `HALT` (`0xF000`), not zeros.
- Programs are written in assembly (`firmware/*.asm`) and turned into `src/rom.v` by
  `tools/build_rom.sh`, which assembles `firmware/main.asm` (boot dispatcher), `uart_tx.asm`,
  `spi_master.asm`, `i2c_master.asm`, `i2c_monitor.asm` and `echo.asm` into one ROM (158 words).
  Labels are global across files.

## How the hardware does it

Two pipeline stages: in one clock the instruction of the thread whose turn it is gets fetched; in the next it
executes. Consecutive instructions belong to different threads, so there are never any data hazards, and a
thread's next fetch is three clocks after its execute, so it always sees its own updated `PC`. This is
why the machine runs at the full clock with no forwarding logic and no branch penalty.

## Verification

The instruction set above is also written as a separate reference model (`test/isa_model.py`) that knows nothing about the
pipeline. `test/random_test.py` runs random programs (every instruction, random branch targets, `SPAWN` and the channel
included) on the model and on the RTL and compares every architectural register of every thread after every clock edge.
