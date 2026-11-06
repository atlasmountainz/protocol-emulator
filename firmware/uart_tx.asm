; UART 8N1 transmitter, 434 clocks per bit (50 MHz / 115200 baud).
;
; Input pins : 0-7 = ui_in (data byte), 8 = uio_in[0] (start, level-triggered)
; Output pins: 0 = TX, 1 = BUSY
;
; Cycle budget: every instruction takes 1 cycle, WAIT n takes 1+n.
; Per data bit: SHR(1) + OUTC(1) + WAIT 430 (431) + DJNZ(1) = 434 cycles.

        PIN  0 1              ; TX idles high
        PIN  1 0              ; not busy
idle:   JPIN 8 0 idle         ; spin while start is low
        IN                    ; A = data byte
        PIN  1 1              ; busy
        PIN  0 0              ; start bit
        LDX  8                ; 8 data bits
        WAIT 430              ; PIN..OUTC spacing = 434 cycles
bit:    SHR                   ; C = next data bit (LSB first)
        OUTC 0                ; TX = C
        WAIT 430
        DJNZ bit
        NOP                   ; pad: loop path runs SHR+OUTC (2 cycles), exit path PIN+NOP
        PIN  0 1              ; stop bit
        WAIT 430
rel:    JPIN 8 1 rel          ; wait for start to be released
        PIN  1 0              ; not busy
        JMP  idle
