; SPI master, mode 0, MSB first, 16-bit frame, 8.33 MHz SCK: two threads in lock step. A faster variant of
; firmware/examples/spi_master_fast.asm with the same pins, the same START / BUSY handshake and the same
; frame (send ui[7:0], receive a byte, send it back). Loaded into the program memory, it is not in the ROM.
; Build the image with the schedule 0,1,0,1 (byte 512 = 0x44):
;     python3 tools/load_image.py --sched 0,1,0,1 build/spi_master_turbo firmware/examples/spi_master_turbo.asm
; Documentation: docs/examples-fast-and-memory.md ("Beyond 5 MHz"). Test: test/test_spi_master_fast.py.
;
; Pins as in spi_master_fast.asm: input 0-7 = ui[7:0], 8 = uio[0] START, 9 = uio[1] MISO; output 0 = CS_N,
; 1 = SCK, 2 = MOSI, 3 = BUSY. The loader pins uio[5] / uio[4] are never used.
;
; Why two threads. A bit needs five instructions (SCK rise, SCK fall, MISO in, shift, MOSI out). One thread
; executes one per 2 clocks, so 10 clocks (5 MHz). With the schedule 0,1,0,1 threads 0 and 1 BOTH run one
; instruction every 2 clocks, thread 0 on the odd clocks and thread 1 on the even ones, strictly alternating and
; for ever, whatever the program does. So the two threads can be written as one program split in two columns
; that execute in lock step, with no handshake at all: three instructions each per bit = 6 clocks = 120 ns.
;
;   clock   thread 0 (SCK)      thread 1 (data)
;     0     PIN 1 1                        SCK rises: the slave samples MOSI
;     1                         INB 9      A[0] = MISO of this bit (the pad as it was 2 clocks earlier)
;     2     PIN 1 0                        SCK falls: the slave shifts its next MISO bit out
;     3                         SHL        C = next bit to send (A is the shift register in both directions)
;     4     NOP
;     5                         OUTC 2     MOSI = C, one clock before the next rising edge
;
; SCK is high 2 clocks (40 ns) and low 4 (80 ns); MOSI changes 1 clock before and 5 clocks after a rising
; edge, always while SCK is low. The price of the speed is margin: MOSI setup is the specified minimum of 1 clock
; (20 ns) and the slave has 2 clocks (40 ns) after SCK falls to present its next MISO bit.
;
; The lock step needs both threads to take turns every 2 clocks: it only works with the schedule 0,1,0,1 (8.33 MHz)
; or with the default schedule 0,1,2,3 (the same program at half the rate, 4.17 MHz). With 0,1,0,2 thread 1 would
; run at half speed and the data would be garbage.

spi_start:
        PIN  0 1              ; CS_N idle high
        PIN  1 0              ; SCK idle low
        PIN  3 0              ; not busy
spi_idle:
        JPIN 8 0 spi_idle     ; spin while START is low
        PIN  3 1              ; busy
        PIN  0 0              ; assert CS_N
        SPAWN 1 data          ; thread 1 starts: its first instruction runs 2 instructions of this thread later
        NOP
        NOP
; ---- thread 0: three instructions per bit; SCK rises on the second
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        NOP
        PIN  1 1
        PIN  1 0
        WAIT 1                ; CS hold: 3 turns from the last falling edge to CS_N
        PIN  0 1              ; deassert CS_N
        PIN  2 0              ; MOSI low
spi_rel:
        JPIN 8 1 spi_rel      ; wait for START to be released
        PIN  3 0              ; not busy
        JMP  spi_idle

; ---- thread 1: the data path (started by SPAWN, stops itself after the frame)
data:
        IN                    ; A = byte to send
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        SHL
        OUTC 2
        INB  9
        HALT
