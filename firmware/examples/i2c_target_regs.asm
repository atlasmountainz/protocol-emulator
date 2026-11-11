; I2C target (slave) at address 0x2A with 16 read/write registers that live in the thread's data memory.
; Loaded into the program memory, it is not in the ROM. Build the image with:
;     python3 tools/load_image.py build/i2c_target_regs firmware/examples/i2c_target_regs.asm
; (add --sched 0,1,0,2 for twice the speed: nothing in the program depends on the schedule).
; Documentation: docs/examples-fast-and-memory.md. Test: test/test_i2c_target_regs.py.
;
; Input pins : 8 = SDA (uio[0] pad read-back), 9 = SCL (uio[1]); the master drives SCL, never stretched.
; Open-drain : pin 0 = SDA (uio[0]): ODRV 0 0 pulls it low, ODRV 0 1 releases it.
; Output pins: uo[7:0] = the last byte written to any register (0x00 after loading).
; Never used : uio[5], uio[4] (the loader's LOAD / STROBE).
;
; Transactions (standard I2C, MSB first), like firmware/examples/i2c_target.asm:
;   write : START, 0x54 (0x2A + W), register pointer, data bytes ..., STOP or repeated START
;   read  : START, 0x55 (0x2A + R), bytes ... (master ACKs all but the last), STOP
;   Any other address is not acknowledged and ignored until the next START.
;
; Registers: reg[0..15], MEM[0..15] of thread 0, 0x00 after loading. The first byte after the write address
; is the register pointer (only its low 4 bits count: the memory address is (X + offset) mod 16, so the high
; nibble is ignored for free). Every following data byte of the write goes to reg[pointer], and the pointer
; then moves on by one, 15 -> 0. A read returns reg[pointer], reg[pointer + 1], ... the same way (the pointer
; also moves after a byte the master NACKs). The pointer survives STOP and is only changed by a pointer byte.
;
; What the new instructions replace in i2c_target.asm (181 words): the pointer is X, not a program-counter
; chain of 16 states with a 16-leaf decision tree; the register file is memory (LDMX / STMX), not 16 LDAs; the
; address is compared with XOR / JZ instead of seven JPIN steps; bits are counted by a sentinel bit in A (SHL
; shifts it out into C, JNC loops) so no DJNZ counter and no mode flag in X is needed; and the byte shown on
; uo[7:0] is one OUTA instead of eight SHR / OUTC pairs.
;
; Timing: no delays at all, every wait is a poll of SCL / SDA (1 turn per poll: 4 clocks with the default
; schedule, 2 clocks with 0,1,0,2), so the target follows the master at any SCL rate that leaves it time to
; work.

start:
        OUT  0x00             ; uo[7:0] = 0
        LDX  0                ; pointer = 0

; ---- idle: wait for a START (SDA falls while SCL is high). Also the place to go after a STOP or a NACK.
idle:
        JPIN 9 0 idle         ; SCL low: nothing can happen
        JPIN 8 0 i_lo         ; SCL high and SDA low
i_hi:
        JPIN 9 0 idle         ; SCL high and SDA high: wait for SDA to fall
        JPIN 8 1 i_hi
        JPIN 9 0 idle         ; re-check: SCL fell as well, so SDA only started the next data bit

; ---- address phase, after a START: eight bits into A, then compare
a_phase:
        LDA  1                ; sentinel in bit 0: shifted out into C after the 8th bit
        CALL rx_loop
        XOR  0x54             ; 0x2A << 1 | W: zero for the write address
        JZ   a_wr
        DEC                   ; 0x55 (read) -> zero
        JZ   a_rd
        JMP  idle             ; not our address: leave SDA alone = NACK
i_lo:
        JPIN 9 0 idle         ; SCL high and SDA low: wait for SDA to rise (a STOP)
        JPIN 8 0 i_lo
        JMP  i_hi

; ---- write: the first byte is the pointer, then data bytes until STOP or a repeated START
a_wr:
        CALL ack
        ODRV 0 1
        CALL rx_byte          ; pointer byte
        TAX                   ; X = pointer (the memory only looks at its low 4 bits)
        CALL ack
        ODRV 0 1
w_loop:
        CALL rx_byte          ; data byte
        STMX 0                ; reg[pointer] = byte
        XADD 1                ; pointer + 1
        OUTA                  ; uo[7:0] = byte
        CALL ack
        ODRV 0 1
        JMP  w_loop

; ---- read: send reg[pointer], reg[pointer + 1], ... until the master NACKs
a_rd:
        JPIN 9 1 a_rd         ; wait for SCL low (the 8th bit is over)
        ODRV 0 0              ; ACK the address
        LDMX 0                ; A = first register
        XADD 1
a_rh:
        JPIN 9 0 a_rh         ; the ACK clock: SCL high ...
a_rl:
        JPIN 9 1 a_rl         ; ... and low again: bit 7 follows at once
rd_bits:
        CALL txbit
        CALL txbit
        CALL txbit
        CALL txbit
        CALL txbit
        CALL txbit
        CALL txbit
        CALL txbit
        ODRV 0 1              ; release SDA: the master's ACK / NACK
rd_a:
        JPIN 9 0 rd_a         ; SCL high
        JPIN 8 1 idle         ; SDA high = NACK: that was the last byte, wait for STOP / START
        LDMX 0                ; ACK: the next register, loaded while the ACK clock finishes
        XADD 1
rd_w:
        JPIN 9 1 rd_w         ; SCL low: the next byte starts
        JMP  rd_bits

; ---- txbit: send the next bit of A (MSB first); SDA changes right after SCL fell, returns after the next fall
txbit:
        SHL                   ; C = next bit
        ODRVC 0               ; SDA = C: low if 0, released if 1
tb_h:
        JPIN 9 0 tb_h         ; SCL high
tb_l:
        JPIN 9 1 tb_l         ; SCL low
        RET

; ---- rx_byte: SCL is low (an ACK bit has just ended). Returns with A = the next byte. Bit 7 is where a STOP or a
; ---- (repeated) START can appear: SDA changes while SCL is high. A change that is followed by SCL falling at once
; ---- is only the next data bit (re-checked). STOP leaves via idle, START via the address phase (the call is
; ---- simply abandoned: the 4-entry return stack is circular).
rx_byte:
r7:
        JPIN 9 0 r7           ; wait for SCL high
        JPIN 8 1 r7_hi
r7_lo:
        JPIN 9 0 r7_d0        ; bit 7 = 0: SCL fell
        JPIN 8 1 r7_stp       ; SDA rose
        JMP  r7_lo
r7_stp:
        JPIN 9 0 r7_d0
        JMP  idle             ; STOP
r7_hi:
        JPIN 9 0 r7_d1        ; bit 7 = 1: SCL fell
        JPIN 8 0 r7_sta       ; SDA fell
        JMP  r7_hi
r7_sta:
        JPIN 9 0 r7_d1
        JMP  a_phase          ; (repeated) START
r7_d0:
        LDA  2                ; bit 7 and the sentinel one place above it
        JMP  rx_loop
r7_d1:
        LDA  3
; ---- rx_loop: A holds the bits so far under a sentinel 1; every pass shifts one more bit in at the bottom.
; ---- The pass whose SHL pushes the sentinel out (C = 1) is the one for the last bit.
rx_loop:
        SHL
        CALL bitclk           ; wait for the next rising edge of SCL
        INB  8                ; A[0] = SDA
        JNC  rx_loop
        RET

; ---- ack: after the 8th bit, wait for SCL low, pull SDA low for the 9th clock pulse, return when SCL is low again
; ---- (the caller releases SDA)
ack:
        JPIN 9 1 ack
        ODRV 0 0
ack_h:
        JPIN 9 0 ack_h
ack_l:
        JPIN 9 1 ack_l
        RET

; ---- bitclk: wait for SCL low, then for SCL high: returns just after a rising edge
bitclk:
        JPIN 9 1 bitclk
bc_h:
        JPIN 9 0 bc_h
        RET
