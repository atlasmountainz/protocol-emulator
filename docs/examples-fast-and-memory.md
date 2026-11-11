# Examples: a 5 MHz SPI master and an I2C target with its registers in data memory

Two programs that use what instruction set version 2 added (`docs/isa.md`): the loadable thread **schedule** (byte 512 of
the load, `docs/loading.md`) for the SPI master, and the ALU, the conditional branches and the 16-byte data memory
for the I2C target. Like the other examples they are loaded into the program memory over the pins; none of them is in
the ROM. Each has a cocotb module that loads it through the real pin loader and drives it with a strict protocol model.

| Program | Role | Words | Schedule | Test module |
|---|---|---|---|---|
| `firmware/examples/spi_master_fast.asm` | SPI master, mode 0, **5 MHz** SCK, the ROM master's frame and pins | 93 | `0,1,0,2` (also works with the default, at half the rate) | `test_spi_master_fast.py` |
| `firmware/examples/spi_master_turbo.asm` | the same with two threads in lock step, **8.33 MHz** SCK | 112 | `0,1,0,1` (or the default, 4.17 MHz) | `test_spi_master_fast.py` |
| `firmware/examples/i2c_target_regs.asm` | I2C target at 0x2A, 16 read/write registers in the data memory | 84 | any | `test_i2c_target_regs.py` |

```
python3 tools/load_image.py --sched 0,1,0,2 build/spi_master_fast  firmware/examples/spi_master_fast.asm
python3 tools/load_image.py --sched 0,1,0,1 build/spi_master_turbo firmware/examples/spi_master_turbo.asm
python3 tools/load_image.py                 build/i2c_target_regs  firmware/examples/i2c_target_regs.asm
```

`--sched` appends the schedule byte to the 512 program bytes; the testbench loader takes it as `load_program(..., sched=(0,1,0,2))`.
All times below are measured in the tests from edge timestamps (one clock = 20 ns at 50 MHz). "Turn" means one instruction
of a thread: 4 clocks with the default schedule, 2 clocks for a thread that owns two slots of the schedule.

## 1. SPI master at 5 MHz (`spi_master_fast.asm`)

### Pins

The same as the ROM SPI master (`docs/info.md`), so the same slave and the same test setup work.

| Pin | Direction | Meaning |
|---|---|---|
| `ui[7:0]` | in | the byte to send, read once when START is seen |
| `uio[0]` | in | START, level-triggered: hold it high until BUSY rises, release it to arm the next transfer |
| `uio[1]` | in | MISO |
| `uo[0]` | out | CS_N (high when idle) |
| `uo[1]` | out | SCK (low when idle, mode 0) |
| `uo[2]` | out | MOSI (low when idle) |
| `uo[3]` | out | BUSY: high from just before CS_N falls until START has been released |

`uio[5]` and `uio[4]` are the loader's LOAD / STROBE and are never used. No `uio` pad is driven.

### What it does

One frame of 16 bits, MSB first, mode 0 (SCK idles low, MOSI changes while SCK is low, both sides sample on the rising
edge): the first byte is the one on `ui[7:0]`, while the slave's first byte comes in. The second byte sends the received
byte straight back, so the slave sees `[your byte][its own byte]`. A START pulse of 3 clocks (60 ns) is enough (tested).

### How it reaches 5 MHz

Only thread 0 runs. With the schedule `0,1,0,2` it owns slots 0 and 2, so it executes one instruction every 2 clocks
(40 ns) instead of every 4. One bit is five instructions and the 16 bits are written out one after the other:

```
        PIN  1 1      ; SCK rises: the slave samples MOSI
        INB  9        ; A[0] = MISO (the pad as it was 2 clocks earlier, that is at the rising edge)
        PIN  1 0      ; SCK falls: the slave shifts its next bit out
        SHL           ; C = next bit to send; A is the shift register for both directions
        OUTC 2        ; MOSI = C
```

Five instructions of 2 clocks are 10 clocks, 200 ns, 5 MHz. A loop counter (`DJNZ`) would be a sixth instruction per
bit, 12 clocks and 4.17 MHz, which is why the bits are not looped: 93 words of the 256 are used. After the 8th bit `A`
holds the received byte, and the same `SHL`s send it back. Everything else (CS_N, BUSY, START) is the ROM program's
handshake with its `WAIT`s shortened.

The program has no timing constants except one `WAIT 1`, so loaded with the **default schedule** it runs unchanged at
half the rate (2.5 MHz, every duration doubled). That costs nothing and is tested.

### Measured timing

| | ROM master | `spi_master_fast`, `0,1,0,2` | same, default schedule | `spi_master_turbo`, `0,1,0,1` | turbo, default |
|---|---|---|---|---|---|
| SCK period (frequency) | 960 ns (1.04 MHz) | **200 ns (5.00 MHz)** | 400 ns (2.5 MHz) | **120 ns (8.33 MHz)** | 240 ns (4.17 MHz) |
| SCK high / low | 400 / 560 ns | 80 / 120 ns | 160 / 240 ns | 40 / 80 ns | 80 / 160 ns |
| MOSI stable before the rising edge | 320 ns | 40 ns | 80 ns | 20 ns | 60 ns |
| MOSI held after the rising edge | 640 ns | 160 ns | 320 ns | 100 ns | 180 ns |
| CS_N falls to first rising edge | 800 ns | 120 ns | 240 ns | 200 ns | 400 ns |
| last falling edge to CS_N rises | 400 ns | 120 ns | 240 ns | 120 ns | 240 ns |
| BUSY rises to CS_N falls | | 40 ns | 80 ns | 40 ns | 80 ns |
| slave may present its next MISO bit up to | | 100 ns after SCK falls | 200 ns (tested) | 40 ns | |

All 15 periods of a frame are equal (a mutant with one extra `NOP` is caught), MOSI changes only while SCK is low,
and SCK is low and MOSI is low whenever CS_N is high. CS_N stays high for at least 360 ns between frames when the host
re-arms START in the clock after BUSY falls. The test slave changes the MISO pad 20 ns after SCK falls at the earliest and
is delayed from there: a pad that changes up to 100 ns after the fall works at 5 MHz, 120 ns does not.

### Beyond 5 MHz: two threads in lock step (`spi_master_turbo.asm`)

Five instructions per bit (rise, fall, MISO in, shift, MOSI out; no instruction combines two of them) are the floor I found,
so 5 MHz is the limit of one thread, and of `spi_master_fast`. The schedule `0,1,0,1` gives threads 0 and 1 one instruction every 2 clocks each, strictly
alternating for ever, so two columns of straight-line code run in lock step with no handshake at all:

| clock in a bit | thread 0 (clock) | thread 1 (data) |
|---|---|---|
| 0 | `PIN 1 1` (SCK rises) | |
| 1 | | `INB 9` |
| 2 | `PIN 1 0` (SCK falls) | |
| 3 | | `SHL` |
| 4 | `NOP` | |
| 5 | | `OUTC 2` (MOSI, 1 clock before the next rising edge) |

Three instructions each per bit make 6 clocks, 8.33 MHz. The two threads are aligned by the number of `NOP`s after
`SPAWN`; thread 1 reads `ui[7:0]` itself and halts after the frame. The price is margin: MOSI setup is the specified
1 clock (20 ns), and the slave has only 40 ns after SCK falls to present its next MISO bit. It also needs both threads
at the same rate: with the default schedule it runs at 4.17 MHz, and with `0,1,0,2` thread 1 would run at half speed
and the frame is wrong (a test shows that). With five instructions per bit shared by two threads that execute every
2 clocks, 6 clocks per bit (8.33 MHz) is the best this structure can do.

### What the new features bought

The ROM master spends 12 turns of 4 clocks on a bit, 6 of them in `WAIT`s that stretch the clock phases to 400 ns and
560 ns. Dropping the waits and unrolling leaves 5 instructions; the schedule halves the time of each one. Nothing
here needs an ALU instruction; the speed comes from the schedule (a factor 2, 2.5 MHz without it) and from the unrolled
loop. Two threads on one schedule are worth another 1.67 times.

### Limits

- Mode 0 only, 16 bits per frame, one slave (CS_N is `uo[0]`).
- SCK is not a 50 % clock (80 / 120 ns at 5 MHz).
- The slave must keep its MISO delay within the figures above, and nothing may sit between chip and slave that adds
  more than the slack (a long cable, a level shifter).
- Tested only in simulation, with the pad timing of an ideal 50 MHz clock.

## 2. I2C target with registers in data memory (`i2c_target_regs.asm`)

The target of `docs/examples-i2c-target.md` had read-only registers computed from a program-counter chain. This one has
**16 read/write registers** in the thread's data memory.

### Pins

| Pin | Direction | Meaning |
|---|---|---|
| `uio[0]` | open-drain I/O | SDA: `ODRV 0 0` pulls low, `ODRV 0 1` releases, read back as input pin 8 |
| `uio[1]` | input | SCL (input pin 9). The program never drives it: **no clock stretching** |
| `uo[7:0]` | out | the last byte written to any register (`0x00` after loading) |

SDA and SCL need pull-ups. `uio[5]` and `uio[4]` (the loader) are never used.

### Behaviour

| Transaction | Result |
|---|---|
| `START, 0x54, pointer` | ACK, ACK. The pointer is the low nibble of the pointer byte; the high nibble is ignored |
| `... , data, data, ... STOP` | each byte is ACKed, written to `reg[pointer]`, shown on `uo[7:0]`; the pointer then moves on, 15 wraps to 0 |
| `START` (or repeated START), `0x55`, bytes, NACK, `STOP` | sends `reg[pointer]`, `reg[pointer + 1]`, ... with the same wrap; the byte the master NACKs also moves the pointer |
| any other address | no ACK; the rest of that transaction is ignored |
| `START, 0x54, STOP` | a probe: ACK, nothing changes |

All 16 registers read `0x00` after loading (the load clears the data memory). The pointer is 0 after loading, survives a
STOP, and is changed only by a pointer byte. A repeated START is recognised after the address, the pointer byte and any
data byte.

### How it works

The memory is addressed as `(X + offset) mod 16`, so `X` is the register pointer and the wrap at 16 and the "high nibble
ignored" rule cost nothing. A write is `STMX 0` / `XADD 1`, a read is `LDMX 0` / `XADD 1`:

```
w_loop: CALL rx_byte     ; A = data byte
        STMX 0           ; reg[X mod 16] = A
        XADD 1           ; pointer + 1
        OUTA             ; uo[7:0] = A
        CALL ack
        ODRV 0 1
        JMP  w_loop
```

Bits are counted with a **sentinel**: `A` starts as 1, every bit does `SHL` (which pushes the sentinel one place up and
finally out into `C`), waits for the SCL rising edge, and does `INB 8`; `JNC` loops until the sentinel has left. The
address is received into `A` and compared with `XOR 0x54` / `JZ` (write) and `DEC` / `JZ` (read). Transmitting uses
`SHL` / `ODRVC 0` and eight `CALL txbit`s (X is busy being the pointer, so there is no counter).

What stayed from `i2c_target.asm`, because it was right: every wait is a `JPIN` poll of SCL or SDA, there are no delay
constants, and START / STOP are looked for only at bit 7 of a byte (SDA changing while SCL is high; a change followed by
SCL falling at once is just the next data bit and is re-checked). While idle the program waits for SDA to fall with SCL
high. Leaving a byte by `JMP` on a STOP or START abandons the `CALL` safely, since the 4-entry return stack is circular.

| | `i2c_target.asm` | `i2c_target_regs.asm` |
|---|---|---|
| words | 181 | **84** |
| registers | 16 constants `i XOR 0x5A`, read only | 16 bytes of data memory, read / write |
| register pointer | the program counter: a chain of 16 states and a 16-leaf `JPIN` tree | `X` (`TAX`, `XADD 1`) |
| address compare | 7 `JPIN` steps | `XOR` / `JZ` / `DEC` / `JZ` |
| bit counter and mode | `DJNZ` loop, mode 1..4 kept in `X` | sentinel bit in `A`, `JNC`; the mode is the program counter |
| byte shown on `uo[7:0]` | 8 `SHR` / `OUTC` pairs, 16 slots | one `OUTA` |
| work between the ACK and the next byte | 16 slots (64 clocks) of `SHR` / `OUTC` for `uo[7:0]`: the speed limit of the write path | `STMX`, `XADD`, `OUTA`: 3 turns |

### Timing

Every wait is a poll (one per turn), so the target follows the master. Tested with a master that checks every bit it reads
and writes, with the same bus model as `test_i2c_target.py`:

| SCL | Master timing (20 ns clocks) | Default schedule | Schedule `0,1,0,2` |
|---|---|---|---|
| 100 kHz (Standard) | low 250, high 250 | works | |
| 400 kHz (Fast) | low 65, high 60, tHD;STA = tSU;STA = tSU;STO = 30, tBUF = 65 (the spec minimums) | works | works |
| 588 kHz | period 85, low 44, high 41 | works | |
| 714 kHz | period 70, low 36, high 34 | works | |
| 769 kHz | period 65, low 34, high 31 | fails (*) | |
| 833 kHz | period 60, low 31, high 29 | fails (*) | works |
| 1 MHz | period 50, low 26, high 24 | | works |
| 1.43 MHz | period 35, low 18, high 17 | | works |
| 1.67 MHz | period 30, low 16, high 14 | | fails |
| 2 MHz | period 25, low 13, high 12 | | fails |

(Blank: not run.) At 769 kHz, 833 kHz (default schedule) and 1.67 MHz the failure is a read address that is not acknowledged
after a pointer write and repeated START; at 2 MHz the pointer byte of a write is not acknowledged.
Above 400 kHz the master's START and STOP times shrink with the period (half of the high time: 15 clocks, 300 ns, at the
65 and 60 clock periods), so those rows are not I2C modes. (*) With the 30 clocks (600 ns) that Fast-mode asks for, the
default schedule does follow periods 65 and 60 (769 and 833 kHz) and fails at period 55 (909 kHz) whatever the START time
(`test_start_time_is_the_limit_of_the_default_schedule`). The practical limit is about 714 kHz with the default schedule
(the old target: about 667 kHz) and about 1.4 MHz with `0,1,0,2`; what limits it is the START detection (three polls plus
a re-check before the master pulls SCL low again) and then the time to react to SCL falling. The target never drives SCL: no clock stretching is used or needed at 100 or 400 kHz. It also never
changes SDA while SCL is high, and every change of SDA comes at most 23 clocks (460 ns) after SCL fell with the default
schedule (13 clocks, 260 ns with `0,1,0,2`), inside the Fast-mode limit of 900 ns for tVD;DAT and tVD;ACK. The master
checks that chip-sent data is stable 100 ns before SCL rises.

### Limits

- 7-bit address `0x2A` only: no general call, no 10-bit addressing, no run-time address change.
- All 16 bytes of the data memory are registers, so the program has no scratch memory; its state lives in `A`, `X`, `C`
  and the program counter.
- A START or STOP in the middle of a byte (not at a byte boundary) is not recognised.
- No clock stretching, and the program does not look at SCL being held low by someone else.
- A pointer byte with a non-zero high nibble is accepted and masked.

## Tests

```
cd test && make COCOTB_TEST_MODULES=test_spi_master_fast
cd test && make COCOTB_TEST_MODULES=test_i2c_target_regs
```

`test_spi_master_fast.py` (13 tests) boots the chip, loads the program and the schedule through the pin loader, and
watches `uo[3:0]` every clock. A slave model on MISO (with a settable output delay) and START record every edge; each
frame is checked for 16 SCK pulses, the right bytes on MOSI (byte 2 is the slave's byte 1), exact SCK period, high and
low time, MOSI setup and hold, CS_N setup and hold, BUSY, and idle levels. The tests cover 5 MHz, the default schedule
and `0,1,0,1`, 60 random byte pairs, the slave-delay margin, the START handshake, the ROM master for comparison, and the
turbo program (8.33 MHz, default schedule, slave delay, and a check that it fails with `0,1,0,2`).

`test_i2c_target_regs.py` (28 tests; all numbers at 100 and 400 kHz unless noted) writes patterns to all 16 registers and reads them
back as a full sweep, singly and in halves; reads from the middle with wrap and beyond two turns; writes that wrap; pointer
semantics (nibble, pointer-only write, pointer kept over STOP); repeated START in every place; every single-bit wrong
address; another device's traffic; NACK ending a read; back-to-back transactions; 40 random transactions against a
reference model; a master that changes SDA 20 ns after SCL falls; the schedule `0,1,0,2` at 400 kHz and 1 MHz; the
rate sweep above and its START-time check. Extra bus monitors check that the target never drives SCL, never moves SDA
while SCL is high, and meets tVD.

Mutation checks are part of both modules (`test_mutations_are_caught`): each mutant of the firmware, made in Python,
must fail the tests, and the original must pass the same run.

- SPI master (11 mutants plus 6 for the turbo program): the MISO sample moved three instructions late, `SHL` / `OUTC`
  swapped, a missing `SHL`, MOSI launched after the rising edge, one extra `NOP` (period 12 instead of 10, data still right),
  the CS_N hold removed, BUSY never raised, SCK idling high, a lost falling edge, the loop rolled up with `DJNZ` (correct
  data, 4.17 MHz); for the turbo program thread 1 started one instruction early or late, a lost `NOP`, `SHL` / `OUTC`
  swapped, a missing `INB`, and `INB` in the wrong place.
- I2C target (15 mutants): wrong address bit, `STM` for `STMX`, `STMX 1`, a pointer stride of 2, no pointer advance after
  the first or the later read bytes, the pointer byte ignored, LSB-first reads, SDA not released for the master's
  ACK / NACK, a NACK that is not recognised, a repeated START or STOP that is not recognised, an unacknowledged write byte,
  `uo` showing the wrong register, and an ACK delayed by 21 turns (84 clocks), which still works at 100 kHz and is
  caught at 400 kHz.

Set `SPI_MASTER_FAST_ASM`, `SPI_MASTER_TURBO_ASM` or `I2C_TARGET_REGS_ASM` to run the whole module against a modified
copy of a program.
