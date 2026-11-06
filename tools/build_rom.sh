#!/bin/sh
# Assemble all firmware into src/rom.v. main.asm (the boot dispatcher) must come first.
set -e
cd "$(dirname "$0")/.."
python3 tools/asm.py src/rom.v firmware/main.asm firmware/uart_tx.asm firmware/spi_master.asm
