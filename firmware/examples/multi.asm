; Three protocols at the same time, on four threads, from one program image:
;   threads 0 + 1 : WS2812 LED strip driver (firmware/examples/ws2812.asm, unchanged): DIN on uo[2]
;   thread 2      : I2C target at address 0x2A with 16 read/write registers (firmware/examples/i2c_target_regs.asm,
;                   without its uo[7:0] display): SDA = uio[0], SCL = uio[1]
;   thread 3      : UART transmitter, 115200 baud 8N1 on uo[0]: a status line "UP xx" (xx = a counter in hex)
;                   about every 2 ms
; An example of firmware loaded into the program memory after fabrication; it is NOT in the ROM.
; Build the image with:  python3 tools/load_image.py build/multi firmware/examples/multi.asm
; (default schedule: every thread runs one instruction every 4 clocks = 80 ns). Documentation:
; docs/examples-multi.md. Tests: test/test_multi.py (RTL), firmware/timing/multi_*.timing (proofs).
;
; Pins: uo[2] = WS2812 DIN, uo[0] = UART TX (both push-pull); uio[0] = SDA, uio[1] = SCL (open drain, pull-ups on the
; board). Nothing else is driven. uio[5], uio[4] are the loader's.
;
; Nothing in the hardware divides the pins between the threads: every thread can drive every pin. Each program
; simply uses its own pins (PIN and ODRV change one pin and leave the others alone) and its own data memory, and
; the threads take turns in a fixed order, so no thread's timing depends on what the others do. This is why the
; three programs below are the separate example programs with their labels renamed, and why their timing proofs
; still hold here (firmware/timing/multi_*.timing check them again in this image).

mt_start:
        SPAWN 2 i2c_start
        SPAWN 3 u_start
        JMP  ws_start

; ================================================================ threads 0 + 1: WS2812
; ---- thread 0: the waveform ----------------------------------------------------------------
ws_start:
        PIN  2 0
        SPAWN 1 ws_feed
ws_next:
        RECV                  ; A = next byte (blocks during the latch gap)
        LDX  8
        WAIT 1
ws_bit:
        PIN  2 1              ; slot 0: rising edge
        SHL                   ; slot 1: C = MSB
        WAIT 1                ; slots 2-3
        OUTC 2                ; slot 4: 0 bit falls (T0H = 4 slots)
        WAIT 3                ; slots 5-8
        PIN  2 0              ; slot 9: 1 bit falls (T1H = 9 slots)
        DJNZ ws_more          ; slot 10
        JMP  ws_next          ; slot 11: byte done; RECV is slot 12, bit 0 of the next byte is slot 16
ws_more:
        WAIT 3                ; slots 11-14
        JMP  ws_bit           ; slot 15

; ---- thread 1: the pixel data and the latch gap ---------------------------------------------
ws_gap:
        WAIT 4000             ; line is low and nothing is sent: the strip latches
        LDX  8
        RET

ws_feed:
        LDX  8                ; pixels per frame
ws_c0:
        LDA  0x00
        SEND
        LDA  0xFF
        SEND
        LDA  0x00
        SEND
        DJNZ ws_c1
        CALL ws_gap
ws_c1:
        LDA  0xAA
        SEND
        LDA  0xFF
        SEND
        LDA  0x00
        SEND
        DJNZ ws_c2
        CALL ws_gap
ws_c2:
        LDA  0xFF
        SEND
        LDA  0xAA
        SEND
        LDA  0x00
        SEND
        DJNZ ws_c3
        CALL ws_gap
ws_c3:
        LDA  0xFF
        SEND
        LDA  0x00
        SEND
        SEND
        DJNZ ws_c4
        CALL ws_gap
ws_c4:
        LDA  0xFF
        SEND
        LDA  0x00
        SEND
        LDA  0xAA
        SEND
        DJNZ ws_c5
        CALL ws_gap
ws_c5:
        LDA  0xAA
        SEND
        LDA  0x00
        SEND
        LDA  0xFF
        SEND
        DJNZ ws_c6
        CALL ws_gap
ws_c6:
        LDA  0x00
        SEND
        SEND
        LDA  0xFF
        SEND
        DJNZ ws_c7
        CALL ws_gap
ws_c7:
        LDA  0x00
        SEND
        LDA  0xAA
        SEND
        LDA  0xFF
        SEND
        DJNZ ws_c8
        CALL ws_gap
ws_c8:
        LDA  0x00
        SEND
        LDA  0xFF
        SEND
        LDA  0xAA
        SEND
        DJNZ ws_c0            ; ring closes here
        CALL ws_gap
        JMP  ws_c0

; ================================================================ thread 2: I2C target, 16 registers
i2c_start:
        LDX  0                ; pointer = 0
; ---- i2c_idle: wait for a START (SDA falls while SCL is high). Also the place to go after a STOP or a NACK.
i2c_idle:
        JPIN 9 0 i2c_idle         ; SCL low: nothing can happen
        JPIN 8 0 i2c_i_lo         ; SCL high and SDA low
i2c_i_hi:
        JPIN 9 0 i2c_idle         ; SCL high and SDA high: wait for SDA to fall
        JPIN 8 1 i2c_i_hi
        JPIN 9 0 i2c_idle         ; re-check: SCL fell as well, so SDA only started the next data bit

; ---- address phase, after a START: eight bits into A, then compare
i2c_a_phase:
        LDA  1                ; sentinel in bit 0: shifted out into C after the 8th bit
        CALL i2c_rx_loop
        XOR  0x54             ; 0x2A << 1 | W: zero for the write address
        JZ   i2c_a_wr
        DEC                   ; 0x55 (read) -> zero
        JZ   i2c_a_rd
        JMP  i2c_idle             ; not our address: leave SDA alone = NACK
i2c_i_lo:
        JPIN 9 0 i2c_idle         ; SCL high and SDA low: wait for SDA to rise (a STOP)
        JPIN 8 0 i2c_i_lo
        JMP  i2c_i_hi

; ---- write: the first byte is the pointer, then data bytes until STOP or a repeated START
i2c_a_wr:
        CALL i2c_ack
        ODRV 0 1
        CALL i2c_rx_byte          ; pointer byte
        TAX                   ; X = pointer (the memory only looks at its low 4 bits)
        CALL i2c_ack
        ODRV 0 1
i2c_w_loop:
        CALL i2c_rx_byte          ; data byte
        STMX 0                ; reg[pointer] = byte
        XADD 1                ; pointer + 1
        CALL i2c_ack
        ODRV 0 1
        JMP  i2c_w_loop

; ---- read: send reg[pointer], reg[pointer + 1], ... until the master NACKs
i2c_a_rd:
        JPIN 9 1 i2c_a_rd         ; wait for SCL low (the 8th bit is over)
        ODRV 0 0              ; ACK the address
        LDMX 0                ; A = first register
        XADD 1
i2c_a_rh:
        JPIN 9 0 i2c_a_rh         ; the ACK clock: SCL high ...
i2c_a_rl:
        JPIN 9 1 i2c_a_rl         ; ... and low again: bit 7 follows at once
i2c_rd_bits:
        CALL i2c_txbit
        CALL i2c_txbit
        CALL i2c_txbit
        CALL i2c_txbit
        CALL i2c_txbit
        CALL i2c_txbit
        CALL i2c_txbit
        CALL i2c_txbit
        ODRV 0 1              ; release SDA: the master's ACK / NACK
i2c_rd_a:
        JPIN 9 0 i2c_rd_a         ; SCL high
        JPIN 8 1 i2c_idle         ; SDA high = NACK: that was the last byte, wait for STOP / START
        LDMX 0                ; ACK: the next register, loaded while the ACK clock finishes
        XADD 1
i2c_rd_w:
        JPIN 9 1 i2c_rd_w         ; SCL low: the next byte starts
        JMP  i2c_rd_bits

; ---- i2c_txbit: send the next bit of A (MSB first); SDA changes right after SCL fell, returns after the next fall
i2c_txbit:
        SHL                   ; C = next bit
        ODRVC 0               ; SDA = C: low if 0, released if 1
i2c_tb_h:
        JPIN 9 0 i2c_tb_h         ; SCL high
i2c_tb_l:
        JPIN 9 1 i2c_tb_l         ; SCL low
        RET

; ---- i2c_rx_byte: SCL is low (an ACK bit has just ended). Returns with A = the next byte. Bit 7 is where a STOP or a
; ---- (repeated) START can appear: SDA changes while SCL is high. A change that is followed by SCL falling at once
; ---- is only the next data bit (re-checked). STOP leaves via i2c_idle, START via the address phase (the call is
; ---- simply abandoned: the 4-entry return stack is circular).
i2c_rx_byte:
i2c_r7:
        JPIN 9 0 i2c_r7           ; wait for SCL high
        JPIN 8 1 i2c_r7_hi
i2c_r7_lo:
        JPIN 9 0 i2c_r7_d0        ; bit 7 = 0: SCL fell
        JPIN 8 1 i2c_r7_stp       ; SDA rose
        JMP  i2c_r7_lo
i2c_r7_stp:
        JPIN 9 0 i2c_r7_d0
        JMP  i2c_idle             ; STOP
i2c_r7_hi:
        JPIN 9 0 i2c_r7_d1        ; bit 7 = 1: SCL fell
        JPIN 8 0 i2c_r7_sta       ; SDA fell
        JMP  i2c_r7_hi
i2c_r7_sta:
        JPIN 9 0 i2c_r7_d1
        JMP  i2c_a_phase          ; (repeated) START
i2c_r7_d0:
        LDA  2                ; bit 7 and the sentinel one place above it
        JMP  i2c_rx_loop
i2c_r7_d1:
        LDA  3
; ---- i2c_rx_loop: A holds the bits so far under a sentinel 1; every pass shifts one more bit in at the bottom.
; ---- The pass whose SHL pushes the sentinel out (C = 1) is the one for the last bit.
i2c_rx_loop:
        SHL
        CALL i2c_bitclk           ; wait for the next rising edge of SCL
        INB  8                ; A[0] = SDA
        JNC  i2c_rx_loop
        RET

; ---- i2c_ack: after the 8th bit, wait for SCL low, pull SDA low for the 9th clock pulse, return when SCL is low again
; ---- (the caller releases SDA)
i2c_ack:
        JPIN 9 1 i2c_ack
        ODRV 0 0
i2c_ack_h:
        JPIN 9 0 i2c_ack_h
i2c_ack_l:
        JPIN 9 1 i2c_ack_l
        RET

; ---- i2c_bitclk: wait for SCL low, then for SCL high: returns just after a rising edge
i2c_bitclk:
        JPIN 9 1 i2c_bitclk
i2c_bc_h:
        JPIN 9 0 i2c_bc_h
        RET

; ================================================================ thread 3: UART status line
; MEM[0] = the counter. Every line is "UP xx" CR LF. Per bit: SHR, OUTC, WAIT 104, DJNZ = 108 turns = 432 clocks.
u_start:
        PIN  0 1              ; TX idles high
u_line:
        LDA  0x55             ; 'U'
        CALL u_send
        LDA  0x50             ; 'P'
        CALL u_send
        LDA  0x20             ; ' '
        CALL u_send
        LDM  0
        SWAP
        CALL u_hex            ; high digit
        LDM  0
        CALL u_hex            ; low digit
        LDA  0x0D
        CALL u_send
        LDA  0x0A
        CALL u_send
        LDM  0
        INC
        STM  0
        WAIT 4095             ; about 1.3 ms of idle line between status lines
        JMP  u_line

u_hex:                        ; send the low nibble of A as a hex digit
        AND  0x0F
        CMP  10
        JC   u_dig
        ADD  7                ; 'A'..'F'
u_dig:  ADD  0x30
u_send:                       ; send A (LSB first); A and X are destroyed
        PIN  0 0              ; start bit
        LDX  8
        WAIT 104
u_bit:  SHR
        OUTC 0
        WAIT 104
        DJNZ u_bit
        NOP
        PIN  0 1              ; stop bit
        WAIT 104
        RET
