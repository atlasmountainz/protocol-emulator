; UART receiver, 8N1, 115200 baud at 50 MHz (108 slots = 432 clocks per bit).
; An example of firmware loaded into the program memory after fabrication: it is NOT in the ROM.
; Build the image with:  python3 tools/load_image.py build/uart_rx firmware/examples/uart_rx.asm
;
; Input pins : 0 = ui[0] (RX, idles high)
; Output pins: uo[7:0] = the last byte received (updated bit by bit, a few slots after the stop bit's
;              midpoint)
; Open-drain : pin 7 = uio[7] = RX_VALID, pulled low once a byte is on uo[7:0], released at the next start bit
;
; A start bit is the falling edge on RX. Each bit is sampled in the middle: 1.5 bit times after the edge
; for bit 0, then every 108 slots. The bits are shifted in with SHL / INB, so the first bit received
; ends up in A[7]; shifting A out again with SHL / OUTC puts it on uo[0]. Reading and writing
; both go through the carry, so no bit reversal code is needed.
;
; Per bit: SHL(1) INB(1) WAIT 104 (105) DJNZ(1) = 108 slots.

rx_start:
        OUT  0x00             ; uo = 0
        ODRV 7 1              ; RX_VALID released
rx_idle:
        JPIN 0 1 rx_idle      ; spin while RX is high
        ODRV 7 1              ; new byte starting: not valid
        WAIT 154              ; to the middle of data bit 0 (about 1.5 bit times after the edge)
        LDX  8
rx_bit:
        SHL
        INB  0                ; sample RX into A[0]
        WAIT 104
        DJNZ rx_bit
        SHL
        OUTC 0
        SHL
        OUTC 1
        SHL
        OUTC 2
        SHL
        OUTC 3
        SHL
        OUTC 4
        SHL
        OUTC 5
        SHL
        OUTC 6
        SHL
        OUTC 7
        ODRV 7 0              ; RX_VALID: the byte is on uo[7:0]
rx_stop:
        JPIN 0 0 rx_stop      ; wait for the stop bit (line high)
        JMP  rx_idle
