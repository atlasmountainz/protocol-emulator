; SPI master, mode 0 (CPOL=0, CPHA=0), MSB first, 16-bit frame.
;
; Input pins : 0-7 = ui_in (byte to send), 8 = uio_in[0] (start, level-triggered),
;              9 = uio_in[1] (MISO)
; Output pins: 0 = CS_N, 1 = SCK, 2 = MOSI, 3 = BUSY
;
; A is a full-duplex shift register: each bit shifts the next MSB out to MOSI and
; shifts MISO in at bit 0. After the first 8 bits, A holds the byte received from
; the slave, so the second 8 bits send that byte straight back out on MOSI.
; This makes a built-in self-test: the slave sees [ui_in byte][its own byte echoed].
;
; Per bit: SHL(1) OUTC(1) WAIT 6 (7) PIN(1) WAIT 6 (7) INB(1) PIN(1) DJNZ(1) = 20 cycles.

spi_start:
        PIN  0 1              ; CS_N idle high
        PIN  1 0              ; SCK idle low
        PIN  3 0              ; not busy
spi_idle:
        JPIN 8 0 spi_idle     ; spin while start is low
        IN                    ; A = byte to send
        PIN  3 1              ; busy
        PIN  0 0              ; assert CS_N
        WAIT 6                ; CS setup time
        LDX  16               ; 16 bits
spi_bit:
        SHL                   ; C = next MSB to send
        OUTC 2                ; MOSI = C (SCK is low)
        WAIT 6
        PIN  1 1              ; SCK rising edge: slave samples MOSI
        WAIT 6
        INB  9                ; A[0] = MISO (stable by end of SCK high phase)
        PIN  1 0              ; SCK falling edge: slave shifts MISO
        DJNZ spi_bit
        WAIT 6                ; CS hold time
        PIN  0 1              ; deassert CS_N
        PIN  2 0              ; MOSI low
spi_rel:
        JPIN 8 1 spi_rel      ; wait for start to be released
        PIN  3 0              ; not busy
        JMP  spi_idle
