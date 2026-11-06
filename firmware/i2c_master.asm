; I2C master, standard mode (~100 kHz at 50 MHz), with clock stretching.
; Demo: an EEPROM reader that bridges I2C to UART.
;
; Input pins : 0-7 = ui_in (register / pointer byte), 8 = uio_in[0] (start, level-triggered),
;              10 = SCL, 11 = SDA (read back from the pads, so slave stretching is visible)
; Open-drain : 2 = SCL (uio[2]), 3 = SDA (uio[3]).  ODRV p 0 pulls low, ODRV p 1 releases.
; Output pins: 0 = UART TX, 1 = UART BUSY, 3 = I2C BUSY, 4 = ERR (slave NACKed)
; Mode select: uio_in[6] = 1 at reset.
;
; Transaction (an "EEPROM random read"):
;   START, 0xA0 (addr 0x50 write), ui_in (pointer), REPEATED START, 0xA1 (addr 0x50 read),
;   read one byte, NACK, STOP, then print the byte on the UART.
;
; Timing: every delay is WAIT 240 = 241 cycles = 4.82 us, which meets the standard-mode
; minimums (tLOW 4.7 us, tHIGH 4.0 us, tSU;STA 4.7 us, tHD;STA 4.0 us, tSU;STO 4.0 us, tBUF 4.7 us).
; After every SCL release we wait for the SCL pin to actually go high, so a slave that
; holds SCL low (clock stretching) just makes the transfer slower.

i2c_start_fw:
        PIN  3 0              ; not busy
        PIN  4 0              ; no error
i2c_idle:
        JPIN 8 0 i2c_idle     ; spin while start is low
        PIN  3 1              ; busy
        PIN  4 0              ; clear error
        CALL i2c_start
        LDA  0xA0
        CALL i2c_wr_byte      ; slave address 0x50, write
        IN                    ; A = pointer byte from ui_in
        CALL i2c_wr_byte
        CALL i2c_start        ; repeated START
        LDA  0xA1
        CALL i2c_wr_byte      ; slave address 0x50, read
        CALL i2c_rd_byte      ; A = byte read (master NACKs: last byte)
        CALL i2c_stop
        CALL uart_send        ; bridge: print the byte on the UART
i2c_rel:
        JPIN 8 1 i2c_rel      ; wait for start to be released
        PIN  3 0              ; not busy
        JMP  i2c_idle

; Slave did not ACK: flag the error, release the bus with a STOP, and abandon the call.
i2c_nack:
        PIN  4 1              ; ERR
        ODRV 2 0              ; SCL low
        CALL i2c_stop
        JMP  i2c_rel

; START or repeated START. Works from idle (both lines high) or with SCL low.
i2c_start:
        ODRV 3 1              ; release SDA
        WAIT 240
        ODRV 2 1              ; release SCL
i2c_st_w:
        JPIN 10 0 i2c_st_w    ; wait until SCL is really high
        WAIT 240              ; tSU;STA / bus free
        ODRV 3 0              ; START: SDA falls while SCL is high
        WAIT 240              ; tHD;STA
        ODRV 2 0              ; SCL low
        RET

; STOP. Enter with SCL low.
i2c_stop:
        ODRV 3 0              ; SDA low
        WAIT 240
        ODRV 2 1              ; release SCL
i2c_sp_w:
        JPIN 10 0 i2c_sp_w
        WAIT 240              ; tSU;STO
        ODRV 3 1              ; STOP: SDA rises while SCL is high
        WAIT 240              ; bus free time
        RET

; Send A MSB first, then clock in the slave's ACK. SCL low on entry and exit.
i2c_wr_byte:
        LDX  8
i2c_wb_bit:
        SHL                   ; C = next bit
        ODRVC 3               ; SDA = C while SCL is low
        WAIT 240              ; data setup / SCL low time
        ODRV 2 1              ; SCL high
i2c_wb_w:
        JPIN 10 0 i2c_wb_w
        WAIT 240              ; SCL high time
        ODRV 2 0              ; SCL low
        DJNZ i2c_wb_bit
        ODRV 3 1              ; release SDA for the ACK bit
        WAIT 240
        ODRV 2 1
i2c_wb_aw:
        JPIN 10 0 i2c_wb_aw
        WAIT 240
        JPIN 11 1 i2c_nack    ; SDA still high at the end of SCL high = NACK
        ODRV 2 0
        RET

; Read one byte into A (MSB first), then send NACK (it is the last byte).
i2c_rd_byte:
        ODRV 3 1              ; release SDA so the slave can drive it
        LDX  8
i2c_rb_bit:
        WAIT 240              ; SCL low time
        ODRV 2 1
i2c_rb_w:
        JPIN 10 0 i2c_rb_w
        WAIT 240              ; SCL high time
        SHL                   ; make room
        INB  11               ; A[0] = SDA
        ODRV 2 0
        DJNZ i2c_rb_bit
        WAIT 240              ; NACK bit: SDA stays released (high)
        ODRV 2 1
i2c_rb_nw:
        JPIN 10 0 i2c_rb_nw
        WAIT 240
        ODRV 2 0
        RET
