# Instruction set

16-bit instructions, 8-bit program counter (256 words), cycle-exact execution:
every instruction takes **1 clock**, except `WAIT n`, which takes **1 + n** clocks.

State: `A` (8-bit accumulator/shift register), `X` (8-bit loop counter), `C` (carry bit),
`OUT` (8 output pins).

Open-drain pins `0-7` map to `uio[7:0]`. They are never driven high: `ODRV` either pulls the line
low or releases it, and the external pull-up does the rest. The pad can always be read back as
input pins `8-15`, so a slave holding a line low (I2C clock stretching) is visible to the firmware.

Return stack: 4 entries, circular. Abandoning a call with a `JMP` instead of `RET` is safe.

Input pins: `0-7` = `ui_in[7:0]`, `8-15` = `uio_in[7:0]` (both pass through a 2-flop synchroniser).
Output pins: `0-7` = `uo_out[7:0]`. Output pins reset to `0x01` so a UART TX line idles high.

| Op  | Mnemonic        | Encoding                       | Effect |
|-----|-----------------|--------------------------------|--------|
| 0x1 | `PIN p v`       | `0001 0ppp ---- ---v`          | `OUT[p] = v` |
| 0x2 | `OUT imm8`      | `0010 ---- iiii iiii`          | `OUT = imm8` |
| 0x3 | `WAIT n`        | `0011 nnnn nnnn nnnn`          | stall `n` extra cycles (0..4095) |
| 0x4 | `JMP addr`      | `0100 ---- aaaa aaaa`          | `PC = addr` |
| 0x5 | `JPIN p 1 addr` | `0101 A pppp aaaaaaa` | if `IN[p] == 1` then `PC = addr` (p = 0..15) |
| 0x0 | `JPIN p 0 addr` | `0000 A pppp aaaaaaa` | if `IN[p] == 0` then `PC = addr` |
| 0x6 | `LDA imm8`      | `0110 ---- iiii iiii`          | `A = imm8` |
| 0x7 | `SHR`           | `0111 ---- ---- ----`          | `C = A[0]; A = A >> 1` |
| 0x8 | `OUTC p`        | `1000 0ppp ---- ----`          | `OUT[p] = C` |
| 0x9 | `LDX imm8`      | `1001 ---- iiii iiii`          | `X = imm8` |
| 0xA | `DJNZ addr`     | `1010 ---- aaaa aaaa`          | `X = X - 1; if X != 0 then PC = addr` |
| 0xB | `IN`            | `1011 ---- ---- ----`          | `A = IN[7:0]` |
| 0xC | `SHL`           | `1100 ---- ---- ----`          | `C = A[7]; A = A << 1` |
| 0xD | `INB p`         | `1101 -ppp p--- ----`          | `A[0] = IN[p]` (p = 0..15) |
| 0xE | `CALL addr`     | `1110 ---- aaaa aaaa`          | push `PC+1` on the return stack, `PC = addr` |
| 0xF | `HALT`          | `1111 0000 ---- ----`          | stop until reset |
| 0xF | `NOP`           | `1111 0100 ---- ----`          | nothing |
| 0xF | `RET`           | `1111 0001 ---- ----`          | pop the return stack into `PC` |
| 0xF | `ODRV p v`      | `1111 0010 ---- vppp`          | open-drain pin `p`: `v=0` pulls it low, `v=1` releases it (p = 0..7) |
| 0xF | `ODRVC p`       | `1111 0011 ---- -ppp`          | open-drain pin `p`: pulls low if `C=0`, releases if `C=1` |

Notes:
- `LDX 0` loops 256 times (X wraps to 255 before the first test).
- Unused ROM addresses read as `HALT`.
- ROM is 256 words (8-bit PC). The current firmware (boot, UART, SPI, I2C) uses 120 of them.
- `JPIN` encodes its pin-state in the opcode (`0x5` = jump if high, `0x0` = jump if low), so the
  freed bit 11 becomes address bit 7. Because `0x0000` is a `JPIN p 0` instruction, unused ROM
  words are filled with `HALT` (`0xF000`), not zeros.
- Programs are written in assembly (`firmware/*.asm`) and turned into `src/rom.v` by
  `tools/build_rom.sh`, which assembles `firmware/main.asm` (boot dispatcher), `uart_tx.asm` and
  `spi_master.asm` into one ROM. Labels are global across files.
