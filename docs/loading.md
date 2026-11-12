# Loading a program after fabrication

The chip runs one of two programs:

- **the built-in ROM firmware** (UART, SPI, I2C or UART echo, picked by the mode pins), or
- **a program the host loads into the 256-word program memory**, which replaces the ROM completely.

## Pins

| Pin | Name | Direction | Meaning |
|---|---|---|---|
| `uio[5]` | LOAD | in | high = loading; the CPU is held in reset |
| `uio[4]` | STROBE | in | each rising edge writes the byte on `ui[7:0]` |
| `ui[7:0]` | DATA | in | the byte to write |

## Sequence

1. Reset the chip (`rst_n` low, then high) with LOAD low. This clears the ROM/RAM choice.
2. Raise LOAD.
3. For each byte of the image, in order: set `ui[7:0]`, then raise STROBE, then lower STROBE. Words are
   sent **low byte first**, so word 0 is bytes 0 and 1, word 1 is bytes 2 and 3, and so on. The image is
   256 words = 512 bytes. Optionally send one more byte, **byte 512**, the thread schedule (below). The byte
   pointer wraps after 1024 bytes; bytes 513 to 1023 are ignored.
4. Lower LOAD (set the mode/start pins to the values the program expects at the same time). The CPU
   starts at address 0 about three clocks later.

Raising LOAD again at any time restarts the pointer at 0 and halts the CPU, so a program can be replaced
without a reset. A reset with LOAD low goes back to the ROM firmware. A reset with LOAD held high keeps
the program memory contents, so a program loaded once can be run again without sending it twice, until
the chip loses power.

## Thread schedule (byte 512)

The four threads normally take turns in the order 0, 1, 2, 3 (`docs/isa.md`). Byte 512 of a load replaces that
**schedule**: bits `[2s+1:2s]` of the byte name the thread that owns clock slot `s` (s = 0..3), so the default is
`0xE4` (`11 10 01 00`). A thread may own two slots two clocks apart and then runs at one instruction per 2 clocks
instead of one per 4:

| Byte 512 | Slots | Instruction period |
|---|---|---|
| `0xE4` (default, or no byte sent) | 0 1 2 3 | 80 ns for every thread |
| `0x88` | 0 2 0 2 | thread 0 and thread 2 at 40 ns (threads 1 and 3 never run) |
| `0x84` | 0 1 0 2 | thread 0 at 40 ns, threads 1 and 2 at 80 ns |
| `0x44` | 0 1 0 1 | thread 0 and thread 1 at 40 ns |

A thread that is in no slot never runs. The schedule is illegal if the same thread owns two slots that follow
each other (slot 3 and slot 0 follow each other too); the chip then uses the default instead. The first byte of
every load restores the default, so a program image without byte 512 always runs with the default schedule.
A reset with LOAD held high keeps the schedule together with the program memory; a reset with LOAD low restores
the default. The loader in `test/loader.py` takes the schedule as an optional argument.

## Timing rules for the host

The pins pass through the chip's 2-flop synchroniser, so the loader sees them 2 clocks late. All that is
needed is that `ui[7:0]` is stable from at least 3 clocks before STROBE rises until STROBE has fallen,
and that STROBE is high and low for at least 3 clocks each. At 50 MHz that is 60 ns, which any
microcontroller or FPGA meets with room to spare. Only STROBE's rising edge counts, so holding it high
longer does not write twice.

## Program memory

- 256 words of 16 bits, the same size as the ROM and the whole 8-bit program counter range.
- The memory has no reset. After power-up its contents are undefined, so always send the full 512 bytes.
  `tools/load_image.py` pads the image with `HALT` up to 256 words, so a program that runs off its end stops.
- Everything else (instructions, pins, timing, all four threads) behaves as in `docs/isa.md`. Firmware assembled for
  the ROM can be loaded unchanged: the whole ROM image (162 words) is loaded in one piece by the tests
  (`FROM_RAM=1`, see `test/test.py`, `firmware_image`) and behaves identically.

## Building an image

```
python3 tools/load_image.py build/image firmware/main.asm firmware/uart_tx.asm
# -> build/image.bin (512 bytes, in transmission order) and build/image.hex (256 words)
```

The testbench side of this protocol is `test/loader.py`. The whole directed test suite also runs with the
firmware loaded this way (`cd test && FROM_RAM=1 make`) and gives identical results to the ROM.

## Example: a protocol the chip was not built with

`firmware/examples/uart_rx.asm` is a UART receiver (8N1, 115200 baud at 50 MHz; 432 clocks per bit). It is not in the ROM: the
testbench loads it into the program memory and receives bytes with it, including at +-3% baud-rate error.
RX is `ui[0]`; the received byte appears on `uo[7:0]`, and `uio[7]` (open-drain) is pulled low while a byte
is valid.
