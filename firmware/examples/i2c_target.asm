; I2C target (slave) at address 0x2A with a 16-register file.
; An example of firmware loaded into the program memory after fabrication: it is NOT in the ROM.
; Build the image with:  python3 tools/load_image.py build/i2c_target firmware/examples/i2c_target.asm
; (181 words, so it needs the 256-word program memory). See docs/examples-i2c-target.md.
;
; Input pins : 8 = SDA (uio[0] pad read-back), 9 = SCL (uio[1]); the master drives SCL, no stretching.
; Open-drain : pin 0 = SDA (uio[0]): ODRV 0 0 pulls it low, ODRV 0 1 releases it.
; Output pins: uo[7:0] = the last byte written to the target (0x00 after reset).
;
; Transactions (all standard I2C, MSB first):
;   write : START, 0x54 (0x2A + W), pointer byte, data bytes ..., STOP or repeated START
;   read  : START, 0x55 (0x2A + R), bytes ... (master ACKs all but the last), STOP
;   Any other address is not acknowledged and ignored until the next START.
;
; Registers: reg[i] = i XOR 0x5A for i = 0..15 (read only). The pointer is the low nibble of the pointer byte
; (the high nibble is ignored). The pointer advances after every data byte written or read (also after
; the byte the master NACKs), wraps from 15 to 0, and survives STOP. A written data byte only goes
; to uo[7:0]; it does not change the register file, but it does advance the pointer.
;
; How it works. This ISA has no ALU, no indexed load and no jump on a register, so the register pointer
; is held in the program counter: u0..u15 below are 16 states, one per register, laid out two words apart
;     uP: LDA reg[P] / CALL xfer
; and xfer RETurns into the next state when it has moved one byte, so the pointer advances by falling
; through the chain. The pointer byte steers the PC into the chain with a tree of JPIN on SDA, one level
; per bit (no data value is ever stored). xfer always runs in the frame of the current state, so a
; START, STOP or NACK never loses the pointer: only a new pointer byte jumps to another state.
; X holds the mode that the previous byte left behind: 1 = read next byte, 2 = write next byte,
; 3 = write next byte after acknowledging the pointer byte, 4 = idle, wait for START.
;
; Timing: no delays at all, every wait is a poll of SCL / SDA (1 slot = 4 clocks per poll), so the target
; follows the master at any SCL rate that leaves it time to work: 100 kHz and 400 kHz are fine.

start:  OUT  0x00
        LDX  4                ; idle: wait for a START
u0:     LDA  0x5A
        CALL xfer
u1:     LDA  0x5B
        CALL xfer
u2:     LDA  0x58
        CALL xfer
u3:     LDA  0x59
        CALL xfer
u4:     LDA  0x5E
        CALL xfer
u5:     LDA  0x5F
        CALL xfer
u6:     LDA  0x5C
        CALL xfer
u7:     LDA  0x5D
        CALL xfer
u8:     LDA  0x52
        CALL xfer
u9:     LDA  0x53
        CALL xfer
u10:    LDA  0x50
        CALL xfer
u11:    LDA  0x51
        CALL xfer
u12:    LDA  0x56
        CALL xfer
u13:    LDA  0x57
        CALL xfer
u14:    LDA  0x54
        CALL xfer
u15:    LDA  0x55
        CALL xfer
        JMP  u0

; ---- xfer: one byte for the current state (A = this register's value), dispatch on the mode in X --------
xfer:   DJNZ x_n1             ; mode 1 = read: falls through
        JMP  rd_start
x_n1:   DJNZ x_n2             ; mode 2 = write: falls through
        JMP  wr_start
x_n2:   DJNZ idle             ; mode 3 = write after the pointer byte: falls through; mode 4 = idle
        CALL ack              ; acknowledge the pointer byte the tree has just received
        ODRV 0 1

; ---- write: receive one byte. Bit 7 is where a STOP or a (repeated) START can appear: SDA changes while
; SCL is high. A change that is followed by SCL falling at once is only the next data bit (re-checked).
wr_start:
w7:     JPIN 9 0 w7           ; wait for SCL high
        JPIN 8 1 w7_hi
w7_lo:  JPIN 9 0 w7_dn0       ; bit 7 = 0: SCL fell
        JPIN 8 1 w7_stp       ; SDA rose
        JMP  w7_lo
w7_stp: JPIN 9 0 w7_dn0
        JMP  idle             ; STOP
w7_hi:  JPIN 9 0 w7_dn1       ; bit 7 = 1: SCL fell
        JPIN 8 0 w7_sta       ; SDA fell
        JMP  w7_hi
w7_sta: JPIN 9 0 w7_dn1
        JMP  addr_phase       ; (repeated) START
w7_dn0: LDA  0
        JMP  w_rest
w7_dn1: LDA  1
w_rest: DJNZ w_data           ; X = 0 for a data byte, 1 for the pointer byte (set by the address phase)

; ---- pointer byte: ignore bits 6..4, decode bits 3..0 with the tree below, then enter state u[pointer]
        CALL bitclk
        CALL bitclk
        CALL bitclk
        LDX  3                ; the state will acknowledge the pointer byte

; ---- pointer decode tree: one level per pointer bit (b3 first); each node samples SDA after a rising edge
tree:   CALL bitclk
        JPIN 8 1 t_1
t_0:   CALL bitclk
        JPIN 8 1 t_01
t_00:   CALL bitclk
        JPIN 8 1 t_001
t_000:   CALL bitclk
        JPIN 8 1 u1
        JMP  u0
t_001:   CALL bitclk
        JPIN 8 1 u3
        JMP  u2
t_01:   CALL bitclk
        JPIN 8 1 t_011
t_010:   CALL bitclk
        JPIN 8 1 u5
        JMP  u4
t_011:   CALL bitclk
        JPIN 8 1 u7
        JMP  u6
t_1:   CALL bitclk
        JPIN 8 1 t_11
t_10:   CALL bitclk
        JPIN 8 1 t_101
t_100:   CALL bitclk
        JPIN 8 1 u9
        JMP  u8
t_101:   CALL bitclk
        JPIN 8 1 u11
        JMP  u10
t_11:   CALL bitclk
        JPIN 8 1 t_111
t_110:   CALL bitclk
        JPIN 8 1 u13
        JMP  u12
t_111:   CALL bitclk
        JPIN 8 1 u15
        JMP  u14

; ---- data byte: A = bits so far, then acknowledge it and show it on uo[7:0] while the ACK bit is clocked
w_data: LDX  7
w_bit:  CALL bitclk
        SHL
        INB  8
        DJNZ w_bit
w_f:    JPIN 9 1 w_f          ; 8th bit done: wait for SCL low
        ODRV 0 0              ; ACK
        SHR
        OUTC 0
        SHR
        OUTC 1
        SHR
        OUTC 2
        SHR
        OUTC 3
        SHR
        OUTC 4
        SHR
        OUTC 5
        SHR
        OUTC 6
        SHR
        OUTC 7
w_a:    JPIN 9 0 w_a          ; ACK bit: SCL high
w_b:    JPIN 9 1 w_b          ; SCL low again
        ODRV 0 1              ; release SDA
        LDX  2
        RET


; ---- read: send A, MSB first. SDA changes right after SCL falls; bit 8 is released for the master's ACK/NACK
rd_start:
        JPIN 9 1 rd_start     ; the previous ACK bit is still being clocked: wait for SCL low
rd_bits:
        LDX  8
rd_lp:  SHL                   ; C = next bit
        ODRVC 0               ; C = 0 pulls SDA low, C = 1 releases it
rd_h:   JPIN 9 0 rd_h         ; SCL high
rd_l:   JPIN 9 1 rd_l         ; SCL low
        DJNZ rd_lp
        ODRV 0 1              ; master's turn: ACK (SDA low) or NACK (SDA high)
rd_a:   JPIN 9 0 rd_a
        JPIN 8 1 rd_nack
        LDX  1                ; ACK: the master wants the next byte as well
        RET
rd_nack:
        LDX  4                ; NACK: this was the last byte; wait for STOP / START
        RET

; ---- idle: wait for a START (SDA falls while SCL is high)
idle:   JPIN 9 0 idle         ; SCL low: nothing can happen
        JPIN 8 0 i_lo
i_hi:   JPIN 9 0 idle         ; SCL high and SDA high: wait for SDA to fall
        JPIN 8 1 i_hi
        JPIN 9 0 idle         ; re-check: SCL fell too, so this was only a data bit
        JMP  addr_phase
i_lo:   JPIN 9 0 idle         ; SCL high and SDA low: wait for SDA to rise (a STOP)
        JPIN 8 0 i_lo
        JMP  i_hi

; ---- address phase, after a START: compare the 7 address bits with 0101010 one by one
addr_phase:
        CALL bitclk
        JPIN 8 1 idle         ; bit 6 must be 0
        CALL bitclk
        JPIN 8 0 idle         ; bit 5 must be 1
        CALL bitclk
        JPIN 8 1 idle         ; bit 4 must be 0
        CALL bitclk
        JPIN 8 0 idle         ; bit 3 must be 1
        CALL bitclk
        JPIN 8 1 idle         ; bit 2 must be 0
        CALL bitclk
        JPIN 8 0 idle         ; bit 1 must be 1
        CALL bitclk
        JPIN 8 1 idle         ; bit 0 must be 0   (no match: leave SDA alone = NACK)
        CALL bitclk           ; R/W bit
        JPIN 8 1 a_rd
        CALL ack              ; write: ACK, then the pointer byte follows
        ODRV 0 1
        LDX  1                ; marks the pointer byte for w_rest
        JMP  w7
a_rd:   CALL ack              ; read: ACK, then send the register value in A
        JMP  rd_bits

; ---- subroutines
ack:    JPIN 9 1 ack          ; after the 8th bit: wait for SCL low
        ODRV 0 0              ; pull SDA low = ACK
ack_h:  JPIN 9 0 ack_h        ; the ACK bit's SCL pulse
ack_l:  JPIN 9 1 ack_l
        RET                   ; (the caller releases SDA, or takes over with the first read bit)

bitclk: JPIN 9 1 bitclk       ; wait for SCL low, then for SCL high: returns just after a rising edge
bc_h:   JPIN 9 0 bc_h
        RET
