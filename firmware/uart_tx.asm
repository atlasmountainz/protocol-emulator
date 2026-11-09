; UART 8N1 transmitter, 108 slots = 432 clocks per bit (50 MHz / 115200 baud, 0.5% fast).
;
; Input pins : 0-7 = ui_in (data byte), 8 = uio_in[0] (start, level-triggered)
; Output pins: 0 = TX, 1 = BUSY
;
; uart_send is a subroutine (A = byte to send, A is destroyed) so other protocols can
; use the UART too: the I2C firmware calls it to print the byte it reads.
;
; Timing: every instruction takes 1 slot (4 clocks), WAIT n takes 1+n slots.
; Per data bit: SHR(1) + OUTC(1) + WAIT 104 (105) + DJNZ(1) = 108 slots.

uart_start:
        PIN  0 1              ; TX idles high
        PIN  1 0              ; not busy
idle:   JPIN 8 0 idle         ; spin while start is low
        IN                    ; A = data byte
        CALL uart_send
rel:    JPIN 8 1 rel          ; wait for start to be released
        JMP  idle

uart_send:
        PIN  1 1              ; busy
        PIN  0 0              ; start bit
        LDX  8                ; 8 data bits
        WAIT 104              ; PIN..OUTC spacing = 108 slots
bit:    SHR                   ; C = next data bit (LSB first)
        OUTC 0                ; TX = C
        WAIT 104
        DJNZ bit
        NOP                   ; pad: loop path runs SHR+OUTC (2 slots), exit path PIN+NOP
        PIN  0 1              ; stop bit
        WAIT 104
        PIN  1 0              ; not busy
        RET
