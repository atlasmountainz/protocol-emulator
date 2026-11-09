; PS/2 keyboard (device-to-host) receiver. Scan codes come out of the UART at 115200 baud 8N1.
; Loaded into the program memory after fabrication (it is NOT in the ROM). Build the image with:
;   python3 tools/load_image.py build/ps2_rx firmware/examples/ps2_rx.asm
;
; Input pins : 0 = ui[0] = PS/2 CLK   (idles high)
;              1 = ui[1] = PS/2 DATA  (idles high)
; Output pins: uo[0] = UART TX (idles high; the reset value of the output register is 0x01)
; Open-drain : uio[0] = host "inhibit": pulls the CLK line low. Wire uio[0] to the same net as ui[0]
;              and the keyboard's CLK (the usual 10 k pull-up to 5 V / 3.3 V is on that net).
;              uio[5:4] are the loader's LOAD / STROBE and must stay low.
;
; Frame: 11 bits, each valid on the falling edge of CLK: start (0), D0..D7 (LSB first), odd parity,
; stop (1). CLK is 10 to 16.7 kHz, so a half period is 30 to 50 us.
;
; Two threads, so that a slow UART never makes the receiver miss a clock edge:
;   thread 0 (receiver) : follows CLK, checks start / parity / stop, SENDs the byte (or the error marker)
;   thread 1 (UART)     : RECV, then 8N1 at 108 slots (432 clocks) per bit
;
; The receiver does not sample at the edge itself but 63 slots (5.04 us) later, in the middle of the
; shortest allowed CLK low time (30 us). The keyboard changes DATA 5 us or more before the falling
; edge and holds it until 5 us or more after the rising edge, so every sample point in that window
; is good, which also makes the receiver tolerant of clock jitter.
;
; Bit order. SHL / INB shifts each new bit into A[0], so the first bit on the wire (D0) finishes in A[7].
; The channel carries that wire order and the UART thread shifts it out again with SHL, so
; (as in firmware/echo.asm) no bit reversal is needed. (uart_send in uart_tx.asm shifts with SHR
; and would send the byte backwards, which is why this file has its own transmitter.)
;
; Parity. There is no XOR, so the parity of the data bits is kept in the PROGRAM COUNTER: the data-bit loop
; exists twice, ps2_e (even number of ones so far) and ps2_o (odd), and a 1 on DATA jumps to the other
; one. After the eighth bit the code is in the copy that knows the parity: odd parity needs the parity bit
; to be 1 after an even count and 0 after an odd count.
;
; Errors. A start bit that is not 0, a wrong parity bit or a stop bit that is not 1 sends 0xFE to the
; UART (0xFE is also the PS/2 "resend" code: a real scan code of 0xFE cannot be told from an error) and
; then inhibits the keyboard the way a host does: after the last clock pulse the host pulls CLK low
; for 1501 slots (120 us, the standard asks for at least 100 us), which makes a device abort
; whatever it was sending, then releases it. Good frames never touch CLK.

; ---- thread 0: the receiver --------------------------------------------------------------------
ps2_start:
        ODRV 0 1              ; CLK released (the first slots also let the input synchroniser settle)
        SPAWN 1 ps2_tx
ps2_idle:
        JPIN 0 1 ps2_idle     ; spin while CLK is high: the falling edge of the start bit ends this
        WAIT 62               ; 5 us later DATA is stable
        JPIN 1 1 ps2_bad      ; the start bit must be 0
        LDX  8                ; eight data bits

ps2_e:  JPIN 0 0 ps2_e        ; even number of ones so far: wait for CLK high ...
ps2_e2: JPIN 0 1 ps2_e2       ; ... and then for the next falling edge
        WAIT 62
        SHL
        INB  1                ; A[0] = DATA
        JPIN 1 1 ps2_e_to_o   ; a one flips the parity
        DJNZ ps2_e
        JMP  ps2_e_par
ps2_e_to_o:
        DJNZ ps2_o
        JMP  ps2_o_par

ps2_o:  JPIN 0 0 ps2_o        ; odd number of ones so far
ps2_o2: JPIN 0 1 ps2_o2
        WAIT 62
        SHL
        INB  1
        JPIN 1 1 ps2_o_to_e
        DJNZ ps2_o
        JMP  ps2_o_par
ps2_o_to_e:
        DJNZ ps2_e
        JMP  ps2_e_par

ps2_e_par:                    ; parity bit: must be 1
        JPIN 0 0 ps2_e_par
ps2_e_par2:
        JPIN 0 1 ps2_e_par2
        WAIT 62
        JPIN 1 1 ps2_stop_ok
        JMP  ps2_stop_bad

ps2_o_par:                    ; parity bit: must be 0
        JPIN 0 0 ps2_o_par
ps2_o_par2:
        JPIN 0 1 ps2_o_par2
        WAIT 62
        JPIN 1 0 ps2_stop_ok
ps2_stop_bad:                 ; parity was wrong: still take the stop clock, then report
        JPIN 0 0 ps2_stop_bad
ps2_stop_bad2:
        JPIN 0 1 ps2_stop_bad2
        JMP  ps2_bad

ps2_stop_ok:
        JPIN 0 0 ps2_stop_ok
ps2_stop_ok2:
        JPIN 0 1 ps2_stop_ok2
        WAIT 62
        JPIN 1 0 ps2_bad      ; stop bit must be 1
        SEND                  ; a good frame: A = the scan code, in wire order
ps2_done:
        JPIN 0 0 ps2_done     ; wait for CLK to go high again, then listen for the next frame
        JMP  ps2_idle

ps2_bad:
        JPIN 0 0 ps2_bad      ; wait for the end of the clock pulse
        LDA  0x7F             ; 0xFE as it goes into the channel (wire order = bit-reversed)
        SEND
        ODRV 0 0              ; inhibit: host holds CLK low ...
        WAIT 1500             ; ... 1501 slots = 120 us
        ODRV 0 1              ; release
        JMP  ps2_idle

; ---- thread 1: UART transmitter ---------------------------------------------------------------
ps2_tx:
        RECV                  ; A = byte in wire order
        PIN  0 0              ; start bit
        LDX  8
        WAIT 104
ps2_tx_bit:
        SHL
        OUTC 0
        WAIT 104
        DJNZ ps2_tx_bit
        NOP
        PIN  0 1              ; stop bit
        WAIT 104
        JMP  ps2_tx
