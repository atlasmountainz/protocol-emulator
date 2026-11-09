; WS2812 / NeoPixel LED strip driver: 8 pixels, GRB order, rotating rainbow, 800 kHz NRZ.
; Loaded into the program memory after fabrication (it is NOT in the ROM). Build the image with:
;   python3 tools/load_image.py build/ws2812 firmware/examples/ws2812.asm
;
; Output pins: uo[2] = DIN of the strip (push-pull; low at reset and during the latch gap)
; Input pins : none
;
; Two threads, so the waveform never depends on how the pixel data is produced:
;
;   thread 0 (waveform) : takes one byte at a time from the channel (RECV) and shifts it out MSB first,
;                         exactly 16 slots (1.28 us) per bit, including across byte boundaries.
;   thread 1 (feeder)   : spawned at boot. Generates the pixel bytes with LDA / SEND and, after the last
;                         byte of a frame, sits in WAIT. Thread 0 then finds the channel empty and its
;                         RECV blocks with the line already low: that IS the reset (latch) gap.
;
; Bit timing. One instruction is one slot (80 ns), WAIT n is 1+n slots. Every bit is the same code
; with no data-dependent branch:
;
;   slot  0      PIN 2 1      rising edge, the bit starts
;   slot  1      SHL          C = next data bit (MSB first)
;   slot  2-3    WAIT 1
;   slot  4      OUTC 2       uo[2] = C: a 0 bit falls here (T0H = 4 slots = 320 ns), a 1 stays high
;   slot  5-8    WAIT 3
;   slot  9      PIN 2 0      a 1 bit falls here (T1H = 9 slots = 720 ns)
;   slot 10      DJNZ         last bit of the byte? no: jump to ws_more (WAIT 3, JMP: slots 11-15)
;   slot 11      JMP ws_next  yes: RECV the next byte, LDX 8, WAIT 1 (slots 12-15)
;   slot 16      the next bit's PIN 2 1
;
; Both paths put the next rising edge on slot 16, so the bit period is 16 slots = 1280 ns everywhere.
; (T0L = 960 ns, T1L = 560 ns.) The frame is 8 pixels x 24 bits = 192 bits = 245.8 us.
;
; Latch gap. The feeder starts its WAIT right after its last SEND of the frame is accepted, which is
; when thread 0 takes the second to last byte, about 253 slots before the last bit falls. WAIT 4000
; (4001 slots) therefore leaves the line low for about 3750 slots = 300 us: more than the 50 us that the
; WS2812 needs, and more than the 280 us that newer WS2812B parts ask for.
;
; Animation. There is no data memory, so the colours are code: a ring of NINE colours (40 degree
; hue steps), but a frame is EIGHT pixels. Every frame therefore starts one colour further back in
; the ring and the rainbow moves along the strip by one pixel per frame. The feeder just keeps running
; round the ring; X counts pixels, and the gap is inserted wherever X reaches zero. A byte equal to the
; previous one is sent again without a new LDA (SEND does not change A).
;
; Colours are full brightness (0xFF / 0xAA / 0x00) so that every bit position is exercised;
; for a real strip use smaller constants (8 pixels at 0xFF can draw about 0.5 A).
;
;   pixel   R    G    B      sent as G R B
;   c0     FF   00   00      00 FF 00
;   c1     FF   AA   00      AA FF 00
;   c2     AA   FF   00      FF AA 00
;   c3     00   FF   00      FF 00 00
;   c4     00   FF   AA      FF 00 AA
;   c5     00   AA   FF      AA 00 FF
;   c6     00   00   FF      00 00 FF
;   c7     AA   00   FF      00 AA FF
;   c8     FF   00   AA      00 FF AA

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
