# Example: an SPI target (slave)

`firmware/examples/spi_target.asm` makes the chip answer like a small SPI flash: a JEDEC ID, a read
command with auto-increment, and a status register that counts commands. It is loaded into the program
memory (`docs/loading.md`), it is not in the ROM. Build the image with

```
python3 tools/load_image.py build/spi_target firmware/examples/spi_target.asm
```

It uses 252 of the 256 words. The test is `test/test_spi_target.py`
(`cd test && make COCOTB_TEST_MODULES=test_spi_target`).

## Pins

| Pin | Direction | Use |
|---|---|---|
| `ui[0]` | in | SCK |
| `ui[1]` | in | MOSI |
| `ui[2]` | in | CS_N, active low |
| `ui[3]` | in | not read (a board may wire the mode here, see below) |
| `uo[0]` | out, push-pull | MISO. Low whenever CS_N is high and while the command byte is received |
| `uio[5]`, `uio[4]` | in | the loader's LOAD / STROBE: never used by the firmware |

No other pin is driven. MISO is not tri-stated (the chip has no tri-state output), so several targets
cannot share a MISO line without an external gate.

## Behaviour

MSB first, 8-bit bytes, the first byte after CS_N falls is the command.

| Command | Response |
|---|---|
| `0x9F` | `C2 20 16`, then `FF` for as long as the master keeps clocking |
| `0x03 a` | after the address byte `a`: `table[a & 7]`, `table[(a & 7) + 1]`, ... wrapping from entry 7 to 0, for as long as CS_N stays low. `table[i] = (i*7 + 3) & 0xFF`, i.e. `03 0A 11 18 1F 26 2D 34`. Address bits 7..3 are ignored. MISO is low while the address byte is clocked |
| `0x05` | one status byte: the number of commands seen since start-up, this command included, modulo 32 (so the very first command, if it is `0x05`, reads 1); then `FF` |
| anything else | `FF`, `FF`, ... |

A command counts once all of its 8 bits have been clocked in, whatever it is and whatever follows (a
frame that is only the command byte counts too). A frame cut off before the 8th bit does not count.
Taking CS_N high at any moment, in the middle of a bit or a byte, abandons the frame (the pulse must
last 32 clocks, see below); the next CS_N-low frame starts with a fresh command byte.

### SPI modes

Mode 0 (CPOL = 0, CPHA = 0) and mode 3 (CPOL = 1, CPHA = 1) both sample MOSI on the rising SCK edge and
change MISO on the falling edge; they differ only in the idle level of SCK. The firmware therefore needs no
mode pin: before every rising edge it waits for a falling edge, which is immediate in mode 0 (SCK is
already low) and is the master's first edge in mode 3. `ui[3]` is not read; the test drives it high in
mode 3 to show that it makes no difference.

The only visible difference is the first response bit. A mode 0 target is supposed to have bit 7 ready
before the first rising edge; this one has nothing to say during the command byte (MISO stays low) and
launches the first response bit on the falling edge after the command's last bit in both modes.

## How it works

The CPU has no ADD, no memory and no branch on `A` (only `JPIN` on a pin and `DJNZ` on `X` decide), so
nothing is computed and nothing is stored in a register: **the program counter is the state**.

- **Command decode.** The command byte is matched against `0x9F`, `0x03` and `0x05` by branching on MOSI
  once per bit. A path through the code *is* a partly received command; a mismatch jumps into a chain
  (`u6` .. `u0`) that swallows the remaining bits, so an unknown command costs no extra state. There is no
  bit counter: abandoning a frame is a `JPIN 2 1 idle` from any wait loop.
- **Table.** The low three address bits steer the PC through a three-level tree of `JPIN`s on MOSI to one
  of eight table entries. Each entry is code (`PIN 0 b7`, `LDA`, `LDX 7`, `CALL sb`) and the entries are
  laid out one after another, so falling through is the auto-increment. `(i*7+3)` cannot be computed
  without an adder, so the constants are in the code; eight entries is what fits (each costs 8 words).
- **Shifting.** `sb` sends the rest of a byte with `SHL` / `OUTC 0`, one bit per falling edge. The first
  bit of every response byte is launched by an immediate `PIN 0 v` before `sb` is called, with the byte
  pre-shifted by one for the other seven (that works because the first bit of each byte is known without
  computing: the ID bytes, the table values below 0x80, and a status below 64). Nothing has to be
  computed in the short time between a byte's last rising edge and the next byte's first bit.
- **Pipelining.** `sb` starts by waiting for the previous bit's *rising* edge. Whatever the code does
  between two bytes (`RET`, the next entry's wait) therefore happens while the clock is busy, and a new
  byte starts as fast as the next bit of the same byte. The tail between two bytes must stay short: an
  earlier version that did `LDA` / `LDX` / `CALL` there lost a bit at 38 clocks per bit, which is why the
  ID's second and third bytes are entered like table entries.
- **0xFF filler.** After the payload `ffl_h` / `ffl_l` launch `1` on every falling edge. No shifting needed.
- **Command counter.** Thread 1 (started by `SPAWN 1 ring` at the beginning) walks a ring of `LDA n` /
  `SEND` pairs. The channel is a one-byte mailbox, so thread 1 sits on a `SEND` holding the *next*
  count; thread 0 does one `RECV` per completed command, which releases thread 1 to load the one after
  that. For `0x05` that `RECV` result is the response; for every other command it is thrown away. The
  count is therefore the position of thread 1's program counter in the ring (32 positions, 65 words).
  The `RECV` is the first instruction after the command's 8th bit, before any CS_N check, so a
  command-only frame is counted as well.

## Timing and the highest SCK frequency

One instruction is one slot of 4 clocks (80 ns at 50 MHz). The firmware waits for SCK with a
two-instruction loop (`JPIN 2 1 idle` for CS_N, then `JPIN 0 .. loop` for SCK), so SCK is looked at once
every 2 slots = 8 clocks. A pin change also needs 2 clocks to get through the synchroniser. An SCK edge
is therefore noticed 2 to 10 clocks after it happened, and every time below counts from the edge.

| Path | What has to happen | Worst case | Limit |
|---|---|---|---|
| MISO inside a byte | falling edge noticed (10), `OUTC 0` in the next slot (+4), pin changes (+1) | 15 clocks | SCK low >= 15 + the master's MISO setup |
| First bit after the command | 8th rising edge noticed (10), `RET`, sample `JPIN`, `RECV`, two wait `JPIN`s, `PIN 0 v` (6 slots = 24), pin changes (+1) | 35 clocks | bit time >= 35 + MISO setup |
| MOSI sampling | rising edge noticed (10), `RET` (+4), sample `JPIN` (+4) | sees MOSI as it was up to 16 clocks after the edge (the last 2 clocks of the 18 are the synchroniser) | SCK high >= 16 (master holds MOSI until its falling edge) |

With a master that samples MISO 2 clocks (40 ns) before the rising edge, as the test master does:

| | measured (both modes, 8 phases of the 8-clock poll) |
|---|---|
| SCK low | 17 clocks pass, 16 fail |
| SCK high | 16 clocks pass, 15 fail |
| bit time, symmetric clock | **37 clocks pass, 36 fail** |

So **the highest SCK frequency is 50 MHz / 37 = 1.35 MHz** (duty cycle about 50 %; the first-bit path
needs high + low >= 37, the byte loop needs low >= 17 and the command decode needs high >= 16). The
test runs at 38 clocks per bit (1.316 MHz, 1 clock of margin) and at 37, and also at 40 (1.25 MHz),
50 (1 MHz), 100 (500 kHz) and 200 (250 kHz). With a master that samples MISO exactly at the rising edge
the limit is 36 clocks (1.39 MHz); 35 fails.

Why not faster: the two-instruction wait is what lets CS_N abort every wait. A single-instruction wait
(SCK only) would notice an edge after 2 to 6 clocks instead of 2 to 10, which by the same arithmetic would
allow roughly 1.7 MHz (an estimate, not built), but CS_N could then not end a frame from inside that wait.

### CS_N timing the master must respect

| | Minimum | Why |
|---|---|---|
| CS_N low before the first SCK edge that matters (rising in mode 0, falling in mode 3) | 20 clocks (400 ns) | the code needs 5 slots after CS_N to be waiting for SCK; if the first rising edge arrives earlier in mode 0 it is taken for a falling-edge wait |
| last SCK edge to CS_N rising | 12 clocks (240 ns) | CS_N rising together with the last rising edge could abandon the frame before the command is counted |
| CS_N high between frames | 32 clocks (640 ns) | CS_N is polled every 8 clocks, but the longest stretch of code without a CS_N poll is 24 clocks (the launch of a table byte); a shorter pulse can fall entirely inside it and the frame is not abandoned |
| MISO setup before the master's sampling edge | 2 clocks (40 ns) | as above |

The CS_N-high figure is the one to watch: it is long for an SPI slave. Measured, 20 clocks passes and
12 fails; 32 is the figure with margin.

## Limits

- 8 table entries (the code for each is 8 words); address bits 7..3 are ignored.
- The status counter wraps at 32 (a ring of 32 `LDA / SEND` pairs, 65 words). The ISA has no adder
  and no way to read `X` back, so a counter is a program counter.
- Only the commands listed are decoded; a write or erase command just returns `FF`.
- MISO is push-pull and low when idle, not high-impedance.
- Only thread 0 and thread 1 are used. Threads 2 and 3 are free.

## Test

`test/test_spi_target.py` drives SCK, MOSI and CS_N through `ui_in` with a Python SPI master (any clock
high/low time, mode 0 or 3, adjustable CS_N setup, hold and gap, MISO sampled 2 clocks before the
rising edge) and compares **every MISO bit** of every frame with a bit-level reference model, so
truncated frames are checked as well as complete ones. The setup time grows by one clock from frame to
frame, which walks the clock edges over all 8 phases of the firmware's poll loop.

| Test | What it checks |
|---|---|
| `test_commands_mode0_1mhz`, `test_commands_mode3_1mhz` | every command, 8 table start addresses, reads running past the end of the table, 14 unknown commands, at 1 MHz |
| `test_rates` | the critical commands at all 8 phases, in both modes, at 250 kHz, 500 kHz, 1 MHz, 1.25 MHz, 1.316 MHz (38 clocks) and 1.351 MHz (37 clocks) |
| `test_clock_duty_cycle` | SCK high / low 20 / 17, 16 / 22, 16 / 60 and 60 / 17 clocks, both modes |
| `test_cs_reframing` | CS_N rising after 1..12, 15..17, 23..25, 31..33, 39, 40 bits of an ID, READ, status and unknown frame, each followed by a status read that shows what was counted |
| `test_cs_toggled_without_clocks` | CS_N pulses with no SCK |
| `test_minimum_cs_timing` | CS_N setup 20, hold 12, high 32 clocks at 1 MHz and 1.316 MHz, command-only frames |
| `test_idle_pins` | MISO low when idle, no other output pin and no `uio` pin driven |
| `test_random_frames` | 60 random frames per mode: random commands, lengths, truncation, CS_N timing |
| `test_status_counter_full_cycle` | the status byte over 69 commands: all 32 ring positions, the wrap |

Mutation check (the firmware under test can be replaced with `SPI_TARGET_ASM=<file>`): changing one
constant in the firmware made the matching test fail each time.

| Change | Failing test |
|---|---|
| ID byte `0x16` -> `0x17` (`LDA 0x2C` -> `0x2E`) | `test_commands_mode0_1mhz`: JEDEC ID bit 31 |
| table entry 3 `0x18` -> `0x19` (`LDA 0x30` -> `0x32`) | `test_commands_mode0_1mhz`: READ |
| command decode, `JPIN 1 0 u4` -> `JPIN 1 1 u4` (0x9F no longer matches) | `test_commands_mode0_1mhz`: ID answers `FF` |
| counter ring entry `LDA 5` -> `LDA 6` | `test_status_counter_full_cycle` |
| counter ring wrap `LDA 0` -> `LDA 1` | `test_status_counter_full_cycle` |
| CS_N poll removed from the rising-edge wait of `gb` | `test_cs_reframing`: the cut frame is not abandoned, the next one is misread |
