# Sample testbench for a Tiny Tapeout project

This is a sample testbench for a Tiny Tapeout project. It uses [cocotb](https://docs.cocotb.org/en/stable/) to drive the DUT and check the outputs.
See below to get started or for more information, check the [website](https://tinytapeout.com/hdl/testing/).

## Setting up

1. Edit [Makefile](Makefile) and modify `PROJECT_SOURCES` to point to your Verilog files.
2. Edit [tb.v](tb.v) and replace `tt_um_example` with your module name.

## How to run

To run the RTL simulation:

```sh
make -B
```

To run gatelevel simulation, first harden your project and copy `../runs/wokwi/results/final/verilog/gl/{your_module_name}.v` to `gate_level_netlist.v`.

Then run:

```sh
make -B GATES=yes
```

If you wish to save the waveform in VCD format instead of FST format, edit tb.v to use `$dumpfile("tb.vcd");` and then run:

```sh
make -B FST=
```

This will generate `tb.vcd` instead of `tb.fst`.

## How to view the waveform file

Using GTKWave

```sh
gtkwave tb.fst tb.gtkw
```

Using Surfer

```sh
surfer tb.fst
```

## This project's tests

- `make` runs the directed cocotb tests in `test.py` (UART, SPI, I2C). CI runs the same thing.
- `python3 random_run.py [NUM_SEEDS] [FIRST_SEED]` runs the random differential test: each seed makes a
  random program and random pin inputs, runs them on the RTL and on `isa_model.py` (an independent
  Python model written from `docs/isa.md`), and compares every architectural register on every clock
  edge. It fails if any seed diverges or if any instruction or branch outcome was never exercised.
  `rom_random.v` is a simulation-only ROM that loads the random program from a hex file.
