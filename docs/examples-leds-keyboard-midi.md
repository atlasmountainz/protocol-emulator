# Example firmware: WS2812 LEDs, PS/2 keyboard, MIDI

Three more programs for the loadable program memory (`docs/loading.md`). None of them is in the ROM; each
is one source file in `firmware/examples/`, assembles to well under 128 words, and is tested by
`test/test_leds_keyboard_midi.py`, which loads it through the real pin loader:

```
python3 tools/load_image.py build/ws2812  firmware/examples/ws2812.asm     # 90 words
python3 tools/load_image.py build/ps2_rx  firmware/examples/ps2_rx.asm     # 64 words
python3 tools/load_image.py build/midi_tx firmware/examples/midi_tx.asm    # 48 words
cd test && make COCOTB_TEST_MODULES=test_leds_keyboard_midi
```

All timing below is at the 50 MHz clock: one clock is 20 ns, one instruction slot is 4 clocks = 80 ns, and
`WAIT n` takes `1 + n` slots.

## WS2812 / NeoPixel strip (`ws2812.asm`)

| Pin | Direction | Use |
|---|---|---|
| `uo[2]` | out | DIN of the strip (push-pull, low at reset and between frames) |

The program sends frames of 8 pixels in GRB order, MSB first, 24 bits per pixel, forever.

**Bit timing.** The data line is high for the first part of every bit and low for the rest. The same
eight instructions run for every bit, whether it is a 0 or a 1, and there is no data-dependent branch:

| slot | instruction | effect |
|---|---|---|
| 0 | `PIN 2 1` | rising edge |
| 1 | `SHL` | `C` = next data bit (MSB first) |
| 2-3 | `WAIT 1` | |
| 4 | `OUTC 2` | `uo[2] = C`: a 0 bit falls here, a 1 bit stays high |
| 5-8 | `WAIT 3` | |
| 9 | `PIN 2 0` | a 1 bit falls here |
| 10 | `DJNZ` | more bits in this byte: jump to `WAIT 3` + `JMP` (slots 11-15) |
| 11 | `JMP` | last bit of the byte: `RECV`, `LDX 8`, `WAIT 1` (slots 12-15) |

Both paths put the next rising edge on slot 16. The result, measured in the simulation on every one of the
874 pulses of a four-frame capture, byte boundaries included (every value is the same each time):

| quantity | slots | time | WS2812B window |
|---|---|---|---|
| T0H | 4 | 320 ns | 250-550 ns (task window 220-380 ns) |
| T1H | 9 | 720 ns | 650-950 ns (task window 580-1000 ns) |
| bit period | 16 | 1280 ns | 1050-1450 ns |
| T0L | 12 | 960 ns | 700-1000 ns |
| T1L | 7 | 560 ns | 300-600 ns |

The low times are those of the WS2812B data sheet (T0L 0.85 +-0.15 us, T1L 0.45 +-0.15 us). The original
WS2812 gives T0L as 0.8 +-0.15 us, so 960 ns is 10 ns beyond that; receivers decide on the length of the high
part, so this makes no difference in practice.

**Why two threads.** Thread 0 is only the waveform generator: `RECV` a byte, shift it out. Thread 1 (started
with `SPAWN` at boot) produces the bytes with `LDA` / `SEND`. The pixel data therefore has no influence on
the timing of the waveform: the fixed 4-clock slot makes every pulse exactly the same width, and the producer
can be as slow or as clever as it likes as long as the channel holds the next byte when `RECV` runs at slot 12 of
the last bit (it has 127 slots to deliver it).

**Latch gap.** After the last byte of a frame the feeder executes `WAIT 4000` before the first byte of the next
one. Thread 0 has nothing to receive, so it blocks in `RECV` with the line low: that is the reset. The
`WAIT` starts when the last `SEND` is accepted, which is when thread 0 takes the second to last byte, 253 slots
before the last bit ends, so the line is low for about 3750 slots = 300 us (measured 300.7 us). That is
longer than the 50 us of the WS2812 and the 280 us that newer WS2812B parts ask for. The gap moves by a few
slots depending on which pixel the frame ended on, because the producer takes a different path; it is
not part of the bit timing.

**Frame length and animation.** 8 pixels x 24 bits x 1.28 us = 245.8 us per frame, plus 300 us latch. There is
no data memory, so the colours are in the code: `LDA` / `SEND` for each byte (a byte equal to the previous one
is `SEND`-ed again without an `LDA`). The ring has 9 colours at 40 degree hue steps (0xFF, 0xAA and 0x00 levels, so
that every bit position is exercised in the tests), the frame has 8 pixels, and the producer simply keeps
running around the ring, with `X` counting pixels and the latch gap inserted each time `X` reaches 0. Every
frame therefore starts one colour further back and the rainbow moves along the strip by one pixel per frame
(frame k starts at ring colour 8k mod 9). Use smaller constants for a real strip: 8 white pixels at
0xFF draw about half an ampere.

## PS/2 keyboard receiver (`ps2_rx.asm`)

| Pin | Direction | Use |
|---|---|---|
| `ui[0]` | in | PS/2 CLK |
| `ui[1]` | in | PS/2 DATA |
| `uo[0]` | out | UART TX, 115200 baud 8N1 (idles high) |
| `uio[0]` | open-drain out | host inhibit: pulls CLK low |

`uio[0]` and `ui[0]` are connected to the same net as the keyboard's CLK (with the usual pull-up on that net), so
that the chip can hold the clock low the way a PS/2 host does. The loader's `uio[5]`/`uio[4]` stay low.

**Frames.** Each frame is 11 bits: start (0), D0..D7 LSB first, odd parity, stop (1); a bit is valid on the
falling edge of CLK, and CLK runs at 10 to 16.7 kHz (30 to 50 us per half period). The receiver follows CLK
edge by edge, so the exact clock rate does not matter. It does not sample at the edge but 63 slots (5.04 us)
after it, which is in the middle of the allowed region: the keyboard changes DATA at least 5 us before the
falling edge and holds it until at least 5 us after the rising edge, so with a 30 us low time every sample
point from -5 us to +35 us is good, and the input synchroniser (2 clocks) and the slot (4 clocks) are
negligible next to that.

**Parity without an XOR.** The ISA has no XOR or add, so the parity of the data bits is kept in the program
counter. The data-bit loop exists twice, `ps2_e` (even number of ones so far) and `ps2_o` (odd); a 1 on DATA
(`JPIN 1 1`) continues in the other copy. After the eighth bit the code is already in the copy that knows the
parity, and the parity bit is simply tested: it must be 1 in `ps2_e_par` and 0 in `ps2_o_par`. The data bits
themselves are collected with `SHL` / `INB 1`.

**Threads.** Thread 0 does the PS/2 side; the checked byte goes through the channel to thread 1, which sends it
to the UART. The UART takes 86.8 us per byte. A real keyboard needs at least 660 us per frame, so the UART
is always finished long before the next frame; but if the receiver and the UART were one thread, a frame
that started during the 87 us would be missed. With two threads it is not: the test squeezes frames together
(16.7 kHz clock, only 20 us of idle bus, far less than a keyboard leaves) so that every frame begins while
the UART is still sending the previous byte, and all of them arrive.

**Bit order.** `SHL` / `INB` leave the first bit on the wire in `A[7]`, so `A` is bit-reversed relative to the
byte. Thread 1 therefore shifts out with `SHL` (as `echo.asm` does) and no reversal is needed. The `uart_send`
in `uart_tx.asm` shifts with `SHR`, so it cannot be used on this `A` without reversing the byte first, and the
ISA has no instruction to insert a bit at `A[7]` (see "ISA notes" below); the 8-instruction transmitter in
this file replaces it.

**Errors.** A start bit that is 1, a wrong parity bit or a stop bit that is 0 all send the marker 0xFE to the UART
instead of the byte (a genuine scan code 0xFE, the keyboard's "resend", cannot be told from it). After the
error the chip acts as the host: as soon as CLK is high again it sends the marker and pulls CLK low for 1501
slots (120 us; the standard asks for at least 100 us), which makes any device abort a frame it is still
sending, and then releases it. Good frames never touch the clock line. For a wrong parity bit the receiver still takes the stop
clock first, so the keyboard has finished its frame; for a start bit of 1 the inhibit comes right after the first
clock pulse, in the middle of the frame, which is what makes a real device stop.

**Not implemented.** The host-to-device direction (sending a command such as `0xED` to the keyboard) is not
there, and there is no timeout for a frame that stops half way: the receiver would wait for the next clock
edge, and the next frame would be read from the wrong bit. The inhibit after an error is the only recovery.

## MIDI transmitter (`midi_tx.asm`)

| Pin | Direction | Use |
|---|---|---|
| `ui[7:0]` | in | the byte to send on START |
| `uio[0]` (input pin 8) | in | START, level triggered |
| `uio[1]` (input pin 9) | in | PLAY, level triggered |
| `uo[0]` | out | MIDI TX, 31250 baud 8N1 (idles high) |
| `uo[1]` | out | BUSY |

**Timing.** 31250 baud is exactly 1600 clocks = 400 slots per bit at 50 MHz, so there is no rounding error. Each
data bit is `SHR` (1) + `OUTC` (1) + `WAIT 396` (397) + `DJNZ` (1) = 400 slots; the start bit is `PIN` + `LDX` +
`WAIT 396` + `SHR` = 400 slots up to the first `OUTC`; the stop bit is `PIN` + `WAIT 395` (396) + `RET` + `LDA` +
`CALL` = 400 slots up to the next start bit. The bytes of the built-in sequence therefore follow each other with
no gap: exactly 16000 clocks (320 us) from start edge to start edge, the fastest MIDI stream there is.

**START.** Put a byte on `ui[7:0]` and raise `uio[0]`: the byte goes out once (BUSY is high during it), and
the program waits for `uio[0]` to be released before it looks again, the same way `uart_tx.asm` does.

**PLAY.** Raise `uio[1]` and the program sends `90 3C 64 40 64`, waits for the note length, and sends
`80 3C 40 40 40`. With running status that is four messages in ten bytes: note on C4 (status 0x90, note 60,
velocity 100), note on E4 (no status byte), note off C4 (status 0x80, velocity 64), note off E4 (no
status byte). The note length is `3 x 4096` slots = 983 us so that the simulation stays short; the comment in the
source says how to make it audible. BUSY stays high for the whole sequence, and PLAY, like START, plays only
once however long it is held.

## The tests

`test/test_leds_keyboard_midi.py` (about 3.5 minutes of simulation) has seven tests:

- `test_ws2812_waveform_and_pixels` records every edge of `uo[2]` for four frames, classifies each high
  pulse against the T0H / T1H windows, checks the bit period (1.05-1.45 us), T0L, T1L and the latch gap
  (at least 50 us), requires the widths to be identical from bit to bit (16, 36 and 64 clocks), decodes the
  bits into GRB bytes and compares all four frames with the expected rotating rainbow, and checks that no
  other `uo` pin moves.
- `test_ps2_make_break_10khz`, `test_ps2_16k7_back_to_back`, `test_ps2_clock_jitter`: a keyboard model sends
  frames at 10 kHz (make `1C`, break `F0 1C`, extended `E0 75`), at 16.7 kHz with squeezed frames (see
  above) and single-bit / all-ones / all-zeros bytes, and with random half periods of 30 to 50 us, random
  DATA change times (always at least 5 us from the clock edges) and random gaps. The UART output is
  decoded by a bit-accurate receiver that also requires every edge to lie exactly on the 432-clock grid, and
  each byte must start within 20 us of the stop bit's falling edge (measured: 5.4 us, which is the 5.04 us
  sample delay plus a few slots). The firmware must never touch CLK on good frames.
- `test_ps2_errors`: bad parity, missing stop bit and start bit of 1 each give 0xFE and not the data, each
  causes one CLK inhibit of 100 to 140 us (measured: 120.2 us), a frame that the host aborts is noticed by
  the model, and good frames before and after still arrive.
- `test_midi_raw_bytes`, `test_midi_running_status_sequence`: a receiver on the 1600-clock grid checks six
  raw bytes (including a START held for two frame times), the ten-byte running-status sequence, back-to-back
  spacing of exactly 16000 clocks inside each run, the note length, and BUSY.

### Mutation checks

Each program was changed in one place (a copy under `FW_OVERRIDE_<NAME>`, the real file untouched) and the
tests re-run; every change below made at least one test fail.

| program | change | caught by |
|---|---|---|
| ws2812 | `WAIT 1` -> `WAIT 2` before `OUTC` (T0H 400 ns) | pulse is neither T0H nor T1H |
| ws2812 | `WAIT 3` -> `WAIT 4` before the falling edge of a 1 (period 17 slots = 1.36 us, still inside 1.05-1.45 us) | T0L window (1040 ns); the exact-width check would fail as well |
| ws2812 | one extra `NOP` at the byte boundary (that bit period 17 slots) | T0L window (1040 ns); the exact-width check would fail as well |
| ws2812 | `SHL` -> `SHR` (LSB first) | decoded pixel bytes |
| ws2812 | `WAIT 4000` -> `WAIT 400` (latch gap 12.7 us) | reset gap below 50 us |
| ps2_rx | sample delay `WAIT 62` -> `WAIT 500` (sample after the rising edge) | 16.7 kHz test: wrong bits, frame aborted by the inhibit |
| ps2_rx | parity polarity inverted in `ps2_e_par` | good frame reported as 0xFE |
| ps2_rx | UART `WAIT 104` -> `WAIT 103` in the bit loop | UART edges off the 432-clock grid |
| ps2_rx | stop-bit check removed | missing stop bit delivered as data |
| ps2_rx | start-bit check removed | start bit of 1 delivered as data |
| ps2_rx | inhibit `WAIT 1500` -> `WAIT 1000` (80 us) | inhibit shorter than 100 us |
| midi_tx | data-bit `WAIT 396` -> `WAIT 395` | edges off the 1600-clock grid |
| midi_tx | stop-bit `WAIT 395` -> `WAIT 394` | back-to-back spacing 15996 instead of 16000 clocks |
| midi_tx | `SHR` -> `SHL` (MSB first) | wrong bytes |
| midi_tx | note length `LDX 3` -> `LDX 1` | note length 328 us instead of 983 us |

## ISA notes from writing these

- There is no way to insert a bit at the top of `A`, no rotate, no XOR and no add. A byte assembled with `SHL` /
  `INB` therefore comes out bit-reversed, and anything that needs the opposite order (the UART routine in
  `uart_tx.asm`) needs its mirror image. Because the channel and `A` carry the same wire order, sending
  with `SHL` is free; but it means `uart_send` cannot be reused unchanged for received data.
- A branch on a data value needs either an input pin (`JPIN`) or code duplication (`ps2_e` / `ps2_o`); the
  carry cannot be branched on. `OUTC`/`ODRVC` followed by `JPIN` on the read-back pad would work as a
  "branch on carry", but it wiggles a real pin, so it was not used.
- A thread that blocks in `SEND` / `RECV` costs nothing and pads the timing exactly: the WS2812 latch gap is
  simply a thread that is late with its data.
- `SPAWN` of a running thread does nothing, so one thread cannot restart another; a watchdog for the PS/2
  receiver would need a shared pin or the channel.
