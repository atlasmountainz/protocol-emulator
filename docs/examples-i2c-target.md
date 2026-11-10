# Example: an I2C target (slave) with a register file

`firmware/examples/i2c_target.asm` turns the chip into an I2C **target** at address `0x2A`. The built-in firmware
only contains an I2C *master*; this is the other side of the bus, loaded into the program memory after
fabrication (see `docs/loading.md`). It is 181 words, so it needs the 256-word program memory.

Build and load it like any other example:

```
python3 tools/load_image.py build/i2c_target firmware/examples/i2c_target.asm
```

## Pins

| Pin | Direction | Meaning |
|---|---|---|
| `uio[0]` | open-drain I/O | SDA. Pulled low with `ODRV 0 0`, released with `ODRV 0 1`, read back as input pin 8. External pull-up needed. |
| `uio[1]` | input | SCL, read as input pin 9. The master drives it; the target never stretches the clock. |
| `uo[7:0]` | output | The last data byte written to the target (`0x00` after loading). |

`uio[5]` and `uio[4]` are the loader's LOAD / STROBE and must stay low while the program runs.

## What it does

| Transaction | Bus | Result |
|---|---|---|
| Set pointer | `START, 0x54, pointer` | ACK, ACK. The pointer is the low nibble of the pointer byte (the high nibble is ignored). |
| Write data | `... , data, data, ... STOP` | Each byte is ACKed, shown on `uo[7:0]`, and advances the pointer. |
| Read | `START` (or repeated START), `0x55`, then bytes, master NACKs the last one, `STOP` | Sends `reg[pointer]`, `reg[pointer+1]`, ... |
| Other address | `START, any other address` | No ACK. The target ignores the rest of that transaction and waits for the next START. |
| Probe | `START, 0x54, STOP` | ACK, nothing else happens. |

The register file is `reg[i] = i XOR 0x5A` for `i = 0..15`, so `0x5A, 0x5B, 0x58, 0x59, 0x5E, ...`. It is read-only:
a written byte goes to `uo[7:0]` and does not change a register, but it does advance the pointer, as it would in a
device whose registers were writable. The pointer advances after every byte (also after the byte the master
NACKs), wraps from 15 to 0, and is kept across STOP, so `START, 0x55, ...` alone reads on from where the last
transaction ended. After loading the pointer is 0.

## How it works

This ISA has no ALU, no indexed load and no jump through a register, and nothing can branch on `A`. So the
register pointer is kept in the **program counter**:

```
u0:  LDA 0x5A        ; register 0
     CALL xfer
u1:  LDA 0x5B        ; register 1
     CALL xfer
...
u15: LDA 0x55
     CALL xfer
     JMP u0
```

`xfer` moves one byte (write or read) and `RET`s into the next `LDA`, so the pointer advances by falling
through the chain. The register value is already in `A` when `xfer` starts, so a read has nothing to look up.
A pointer byte is decoded while it arrives: a binary tree of `JPIN 8` (SDA) nodes, one level per bit, ends
in a `JMP` to one of the 16 states. Bits 6 to 4 are clocked and ignored. All the bus code runs inside the
frame of the current state, so a START, STOP or NACK never loses the pointer; only a new pointer byte jumps to
another state.

What the previous byte leaves for the next one is in `X`: 1 = read the next byte, 2 = receive the next data
byte, 3 = acknowledge the pointer byte first, then receive a data byte, 4 = idle (wait for START). The 8
register bits to `uo[7:0]` go out as `SHR` / `OUTC` pairs while the ACK bit is being clocked.

A START or STOP is a change of SDA while SCL is high. It can only happen at the first bit of a byte, so only
bit 7 of a received byte is watched for it; a change that is followed by SCL falling at once is just the next data
bit, and is re-checked. While idle (after a NACK, a mismatched address or a STOP) the firmware waits for SDA to fall
with SCL high.

## Timing

There are no delays: everything is a poll of SDA / SCL (one poll per slot, 4 clocks), so the target follows the
master. Tested in simulation, with a master that checks every bit it reads or writes:

| SCL | Master timing (20 ns clocks) | Result |
|---|---|---|
| 100 kHz (Standard) | low 250, high 250 | works |
| 400 kHz (Fast) | low 65, high 60, tHD;STA = tSU;STA = tSU;STO = 30, tBUF = 65 (the spec minimums) | works |
| 588 kHz | low 45, high 40 | works (in the tests) |
| about 667 kHz | low 40, high 35 | works in a sweep |
| 758 kHz and up | low 36, high 30 | fails: the second data byte of a write is not ACKed |

The limit is the write path: after pulling SDA low for the ACK the target spends 16 slots (64 clocks) updating
`uo[7:0]`, and must be back before the ACK clock pulse is over. Reads and the address phase are faster. So **100 kHz
and 400 kHz are supported with margin; the practical maximum is about 600 kHz** (not an I2C mode). Also tested: a
master that changes SDA only 20 ns after SCL falls.

## Limits

- 7-bit address `0x2A` only: no general call, no 10-bit addressing, no address change at run time.
- No clock stretching, and the target does not look at SCL being held low by someone else.
- A START or STOP in the middle of a byte (not at a byte boundary) is not recognised.
- Written bytes only reach `uo[7:0]`; the 16 bytes read back are fixed in the program.
- A pointer byte with a non-zero high nibble is accepted and masked.

## Test

`test/test_i2c_target.py`: a Python I2C master drives SCL and SDA through the pins (SDA is open-drain: the wire is
low when the master or `uio_oe[0]` pulls it low) and checks ACK / NACK, all 16 pointer values, read data,
auto-increment and wrap, repeated START, wrong addresses (every single-bit variant), back-to-back transactions,
traffic to other devices and a NACKed read ending with the bus released. Most tests run at 100 kHz and 400 kHz.

```
cd test && make COCOTB_TEST_MODULES=test_i2c_target
```

Set `I2C_TARGET_ASM` to a modified copy of the firmware to run the same tests against it. Mutation check: a
changed register constant, a wrong address bit, two swapped pointer-tree leaves, and an ACK delayed by 40 slots
(160 clocks, which still works at 100 kHz and fails at 400 kHz) were each caught by the tests.
