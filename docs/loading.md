# Loading a program after fabrication

The chip runs one of two programs:

- **the built-in ROM firmware** (UART, SPI or I2C, picked by the mode pins), or
- **a program the host loads into the 128-word program memory**, which replaces the ROM completely.

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
   128 words = 256 bytes. The byte pointer wraps after 256 bytes.
4. Lower LOAD (set the mode/start pins to the values the program expects at the same time). The CPU
   starts at address 0 about three clocks later.

Raising LOAD again at any time restarts the pointer at 0 and halts the CPU, so a program can be replaced
without a reset. A reset with LOAD low goes back to the ROM firmware. A reset with LOAD held high keeps
the program memory contents, so a program loaded once can be run again without sending it twice, until
the chip loses power.

## Timing rules for the host

The pins pass through the chip's 2-flop synchroniser, so the loader sees them 2 clocks late. All that is
needed is that `ui[7:0]` is stable from at least 3 clocks before STROBE rises until STROBE has fallen,
and that STROBE is high and low for at least 3 clocks each. At 50 MHz that is 60 ns, which any
microcontroller or FPGA meets with room to spare. Only STROBE's rising edge counts, so holding it high
longer does not write twice.

## Program memory

- 128 words of 16 bits. Addresses 128 to 255 read as `HALT`, so a program that runs off the end stops.
- The memory has no reset. After power-up its contents are undefined, so always send the full 256 bytes.
  `tools/load_image.py` pads the image with `HALT` up to 128 words.
- Everything else (instructions, pins, timing) behaves as in `docs/isa.md`. Firmware assembled for the
  ROM can be loaded unchanged.

## Building an image

```
python3 tools/load_image.py build/image firmware/main.asm firmware/uart_tx.asm
# -> build/image.bin (256 bytes, in transmission order) and build/image.hex (128 words)
```

The testbench side of this protocol is `test/loader.py`. The whole directed test suite also runs with the
firmware loaded this way (`cd test && FROM_RAM=1 make`) and gives identical results to the ROM.
