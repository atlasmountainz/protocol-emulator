; MIDI transmitter: 31250 baud, 8N1, on uo[0]. Two things to do, picked by two level-triggered inputs:
;   uio[0] (input pin 8) = START : send the byte on ui[7:0] as one raw MIDI byte (like uart_tx.asm does)
;   uio[1] (input pin 9) = PLAY  : play a short fixed sequence once (PLAY wins when both are high)
; Loaded into the program memory after fabrication (it is NOT in the ROM). Build the image with:
;   python3 tools/load_image.py build/midi_tx firmware/examples/midi_tx.asm
;
; Input pins : 0-7 = ui[7:0] (the byte for START), 8 = uio[0] (START), 9 = uio[1] (PLAY)
; Output pins: uo[0] = MIDI TX (idles high), uo[1] = BUSY (high while a byte or the sequence is going out)
; (For the usual MIDI current loop put a 220 ohm resistor from uo[0] to pin 5 of the DIN-5 socket
;  and +3.3 / 5 V through 220 ohm to pin 4.)
;
; Timing: 31250 baud at 50 MHz is 1600 clocks = 400 slots per bit, exactly (no rounding error at all).
; Per data bit: SHR(1) + OUTC(1) + WAIT 396 (397) + DJNZ(1) = 400 slots. The start bit is
; PIN(1) + LDX(1) + WAIT 396 (397) + SHR(1) = 400 slots up to the first OUTC.
; The stop bit is PIN 0 1 (1) + WAIT 395 (396) + RET + LDA + CALL (3) = 400 slots up to the next start
; bit, so the bytes of the sequence follow each other with no gap at all: 10 x 400 slots = 4000 slots =
; 320 us per byte, which is the fastest legal MIDI stream.
;
; The sequence uses running status: after the status byte 0x90 (note on, channel 1) the notes need only
; their two data bytes, and the same after 0x80 (note off):
;
;   90 3C 64   note on  C4, velocity 100   (status byte)
;      40 64   note on  E4, velocity 100   (running status: no status byte)
;   ... note length ...
;   80 3C 40   note off C4, velocity 64    (new status byte)
;      40 40   note off E4, velocity 64    (running status)
;
; The note length is 3 x 4096 slots (983 us) so that the whole thing is quick to simulate; for something
; audible, make the LDX in midi_len larger (LDX 0 means 256 passes, 84 ms, and a second loop around it
; makes it longer still).

midi_start:
        PIN  0 1              ; TX idles high
        PIN  1 0              ; not busy
midi_idle:
        JPIN 9 1 midi_play
        JPIN 8 0 midi_idle    ; spin while START is low
        PIN  1 1
        IN                    ; A = ui[7:0]
        CALL midi_send
        PIN  1 0
midi_rel:
        JPIN 8 1 midi_rel     ; wait for START to be released
        JMP  midi_idle

midi_play:
        PIN  1 1
        LDA  0x90
        CALL midi_send
        LDA  0x3C
        CALL midi_send
        LDA  0x64
        CALL midi_send
        LDA  0x40             ; running status: no 0x90 again
        CALL midi_send
        LDA  0x64
        CALL midi_send
midi_len:
        LDX  3
midi_len1:
        WAIT 4095
        DJNZ midi_len1
        LDA  0x80
        CALL midi_send
        LDA  0x3C
        CALL midi_send
        LDA  0x40
        CALL midi_send
        LDA  0x40             ; running status: no 0x80 again
        CALL midi_send
        LDA  0x40
        CALL midi_send
        PIN  1 0
midi_play_rel:
        JPIN 9 1 midi_play_rel
        JMP  midi_idle

; A = byte. A and X are destroyed.
midi_send:
        PIN  0 0              ; start bit
        LDX  8
        WAIT 396
midi_bit:
        SHR                   ; C = next data bit, LSB first
        OUTC 0
        WAIT 396
        DJNZ midi_bit
        NOP                   ; pad: the loop path runs SHR + OUTC (2 slots), the exit path NOP + PIN
        PIN  0 1              ; stop bit
        WAIT 395
        RET
