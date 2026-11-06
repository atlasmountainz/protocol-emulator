# Instruction set

16-bit instructions, 7-bit program counter (128 words), cycle-exact execution:
every instruction takes **1 clock**, except `WAIT n`, which takes **1 + n** clocks.

State: `A` (8-bit accumulator/shift register), `X` (8-bit loop counter), `C` (carry bit),
`OUT` (8 output pins).

Input pins: `0-7` = `ui_in[7:0]`, `8-15` = `uio_in[7:0]` (both pass through a 2-flop synchroniser).
Output pins: `0-7` = `uo_out[7:0]`. Output pins reset to `0x01` so a UART TX line idles high.

| Op  | Mnemonic        | Encoding                       | Effect |
|-----|-----------------|--------------------------------|--------|
| 0x0 | `NOP`           | `0000 ---- ---- ----`          | nothing |
| 0x1 | `PIN p v`       | `0001 0ppp ---- ---v`          | `OUT[p] = v` |
| 0x2 | `OUT imm8`      | `0010 ---- iiii iiii`          | `OUT = imm8` |
| 0x3 | `WAIT n`        | `0011 nnnn nnnn nnnn`          | stall `n` extra cycles (0..4095) |
| 0x4 | `JMP addr`      | `0100 ---- -aaa aaaa`          | `PC = addr` |
| 0x5 | `JPIN p v addr` | `0101 vppp paaa aaaa`          | if `IN[p] == v` then `PC = addr` (p = 0..15) |
| 0x6 | `LDA imm8`      | `0110 ---- iiii iiii`          | `A = imm8` |
| 0x7 | `SHR`           | `0111 ---- ---- ----`          | `C = A[0]; A = A >> 1` |
| 0x8 | `OUTC p`        | `1000 0ppp ---- ----`          | `OUT[p] = C` |
| 0x9 | `LDX imm8`      | `1001 ---- iiii iiii`          | `X = imm8` |
| 0xA | `DJNZ addr`     | `1010 ---- -aaa aaaa`          | `X = X - 1; if X != 0 then PC = addr` |
| 0xB | `IN`            | `1011 ---- ---- ----`          | `A = IN[7:0]` |
| 0xC | `SHL`           | `1100 ---- ---- ----`          | `C = A[7]; A = A << 1` |
| 0xD | `INB p`         | `1101 -ppp p--- ----`          | `A[0] = IN[p]` (p = 0..15) |
| 0xF | `HALT`          | `1111 ---- ---- ----`          | stop until reset |

Notes:
- `LDX 0` loops 256 times (X wraps to 255 before the first test).
- Unused ROM addresses read as `HALT`.
- Programs are written in assembly (`firmware/*.asm`) and turned into `src/rom.v` by
  `tools/build_rom.sh`, which assembles `firmware/main.asm` (boot dispatcher), `uart_tx.asm` and
  `spi_master.asm` into one ROM. Labels are global across files..
