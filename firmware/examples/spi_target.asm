; SPI target (slave), modes 0 and 3, MSB first, up to 1.3 MHz SCK at 50 MHz. Loaded into the program
; memory, it is not in the ROM. Build the image with:
;     python3 tools/load_image.py build/spi_target firmware/examples/spi_target.asm
; Documentation: docs/examples-spi-target.md. Test: test/test_spi_target.py.
;
; Input pins : 0 = ui[0] SCK, 1 = ui[1] MOSI, 2 = ui[2] CS_N (active low). ui[3] is not read.
; Output pins: 0 = uo[0] MISO (push-pull; low whenever it has nothing to say). Nothing else is driven.
; Never used : uio[5], uio[4] (the loader's LOAD / STROBE).
;
; Commands (the first byte after CS_N falls):
;     0x9F          JEDEC ID: C2 20 16, then FF
;     0x03 a        READ: the next bytes are table[(a & 7)], table[(a & 7) + 1], ... wrapping at 8,
;                   with table[i] = (i*7 + 3) & 0xFF. Auto-increments while CS_N stays low.
;     0x05          status = the number of commands seen so far (this one included) modulo 32,
;                   then FF
;     anything else FF, FF, ...
;
; Modes 0 and 3 need no mode pin: both sample MOSI on the rising SCK edge and change MISO on the
; falling edge. Before every rising edge the code waits for a falling edge, which is all that tells
; them apart (SCK idles low in mode 0 and high in mode 3).
;
; How it works, in one paragraph. The CPU has no ADD, no memory and no branch on A, so the state
; lives in the program counter. Thread 0 decodes the command by branching on MOSI at every bit
; (a path through the code is a partly-received command), picks the table entry the same way from
; the address bits, and shifts bytes out with SHL / OUTC. Thread 1 is the command counter: its own
; program counter walks a ring of 'LDA n / SEND' pairs and the channel always holds the next count.
; Every wait loop polls CS_N too, so a rising CS_N abandons the frame at once (no bit counter to
; reset: the PC is the counter, and 'idle' starts it again).
;
; Timing (1 slot = 4 clocks): every wait is a two-instruction loop, so SCK is looked at every 8
; clocks. A response byte is launched on the falling edge of SCK with 'OUTC 0'; the code between two
; bytes runs while the clock is still waiting for the last bit's rising edge ('sb' starts with that
; wait), and the first response bit is launched by an immediate 'PIN 0 v' straight after the last
; command bit.
;
; ---- start-up and idle ---------------------------------------------------------------------
start:
        SPAWN 1 ring          ; thread 1 = command counter
idle:
        PIN  0 0              ; MISO low while not selected
idle_w:
        JPIN 2 1 idle_w       ; spin while CS_N is high

; ---- command byte: 8 bits, one JPIN on MOSI per bit, matched against 0x9F, 0x03, 0x05 ---------
        CALL gb
        JPIN 1 0 c0           ; b7 = 0: candidates 0x03 / 0x05
        CALL gb
        JPIN 1 1 u6           ; 0x9F = 1001 1111: b6 must be 0
        CALL gb
        JPIN 1 1 u5           ; b5 = 0
        CALL gb
        JPIN 1 0 u4           ; b4 = 1
        CALL gb
        JPIN 1 0 u3           ; b3 = 1
        CALL gb
        JPIN 1 0 u2           ; b2 = 1
        CALL gb
        JPIN 1 0 u1           ; b1 = 1
        CALL gb
        JPIN 1 0 u0           ; b0 = 1 (the sample is taken a few slots after the 8th rising edge)
; ---- 0x9F: JEDEC ID = C2 20 16, then 0xFF
        RECV                  ; command complete: step the counter (A discarded)
id_l:
        JPIN 2 1 idle
        JPIN 0 1 id_l       ; SCK high: wait for the falling edge
        PIN  0 1              ; launch bit 7
        LDA  0x84             ; 0xC2 << 1: bit 7 is already on the pin
        LDX  7
        CALL sb
; bytes 2 and 3 are started the way table entries are: wait for the last rising edge, launch bit 7
; at once, then hand the other seven to sb (a LDA / LDX / CALL between two bytes would be too slow)
id2:
        JPIN 2 1 idle
        JPIN 0 0 id2
id2_l:
        JPIN 2 1 idle
        JPIN 0 1 id2_l       ; SCK high: wait for the falling edge
        PIN  0 0              ; launch bit 7
        LDA  0x40             ; 0x20 << 1
        LDX  7
        CALL sb
id3:
        JPIN 2 1 idle
        JPIN 0 0 id3
id3_l:
        JPIN 2 1 idle
        JPIN 0 1 id3_l       ; SCK high: wait for the falling edge
        PIN  0 0              ; launch bit 7
        LDA  0x2C             ; 0x16 << 1
        LDX  7
        CALL sb
        JMP  ffl_h
c0:                           ; 0x03 = 0000 0011, 0x05 = 0000 0101: b6..b3 must be 0
        CALL gb
        JPIN 1 1 u6
        CALL gb
        JPIN 1 1 u5
        CALL gb
        JPIN 1 1 u4
        CALL gb
        JPIN 1 1 u3
        CALL gb
        JPIN 1 1 c5           ; b2 = 1: 0x05 candidate
        CALL gb               ; 0x03: b1 = 1
        JPIN 1 0 u1
        CALL gb               ; b0 = 1
        JPIN 1 0 u0
; ---- 0x03: READ. Address byte follows; the low address bits pick the table entry.
        RECV                  ; command complete: step the counter
        LDX  5
a_skip:
        CALL gb
        DJNZ a_skip
        JMP  t_root
c5:                           ; 0x05: b1 = 0, b0 = 1
        CALL gb
        JPIN 1 1 u1
        CALL gb
        JPIN 1 0 u0
; ---- 0x05: status = number of commands seen (this one included), modulo the ring size
        RECV                  ; A = the counter's value (< 64, so bit 7 is 0)
st_l:
        JPIN 2 1 idle
        JPIN 0 1 st_l       ; SCK high: wait for the falling edge
        PIN  0 0              ; launch bit 7
        SHL                   ; drop the bit just launched
        LDX  7
        CALL sb
        JMP  ffl_h

; ---- anything else: consume the remaining command bits (entry u<r> = r more bits), answer 0xFF
u6:
        CALL gb
u5:
        CALL gb
u4:
        CALL gb
u3:
        CALL gb
u2:
        CALL gb
u1:
        CALL gb
u0:
        RECV                  ; command complete: step the counter
un_l:
        JPIN 2 1 idle
        JPIN 0 1 un_l       ; SCK high: wait for the falling edge
        PIN  0 1              ; launch bit 7
        JMP  ffl_h
; 0xFF forever (until CS_N rises). Entered right after a launch, waiting for that bit's rising edge.
ffl_h:
        JPIN 2 1 idle
        JPIN 0 0 ffl_h
ffl_l:
        JPIN 2 1 idle
        JPIN 0 1 ffl_l
        PIN  0 1
        JMP  ffl_h

; ---- gb: wait for the next SCK rising edge (a falling edge first, so mode 3 works too).
; ---- Every wait polls CS_N as well: if it is high the frame is abandoned.
gb:
        JPIN 2 1 idle
        JPIN 0 1 gb           ; SCK high: wait for it to fall
gb_h:
        JPIN 2 1 idle
        JPIN 0 0 gb_h         ; SCK low: wait for it to rise
        RET

; ---- sb: shift the rest of a response byte out on MISO, MSB first, one bit per SCK falling edge.
; ---- In: A = byte (already shifted left once if its bit 7 is on the pin), X = bits left to send.
; ---- The previous bit's rising edge is still pending, so the loop starts by waiting for it: the
; ---- caller's bookkeeping between two bytes runs while the clock is busy, not on the critical path.
sb:
sb_h:
        JPIN 2 1 idle
        JPIN 0 0 sb_h         ; wait for SCK to rise (the master samples MISO now)
        SHL                   ; C = next bit
sb_l:
        JPIN 2 1 idle
        JPIN 0 1 sb_l         ; wait for SCK to fall
        OUTC 0                ; MISO = C: launched on the falling edge
        DJNZ sb_h
        RET                   ; the last bit's rising edge is pending
; ---- READ address decode: the low 3 address bits steer the PC through a tree, one JPIN on
; ---- MOSI per bit, so the address never has to be held in a register.
t_root:
        CALL gb
        JPIN 1 1 t_1
t_0:
        CALL gb
        JPIN 1 1 t_01
t_00:
        CALL gb
        JPIN 1 1 e1_l
        JMP  e0_l
t_01:
        CALL gb
        JPIN 1 1 e3_l
        JMP  e2_l
t_1:
        CALL gb
        JPIN 1 1 t_11
t_10:
        CALL gb
        JPIN 1 1 e5_l
        JMP  e4_l
t_11:
        CALL gb
        JPIN 1 1 e7_l
        JMP  e6_l

; ---- the 8-entry table: entry i = (i*7+3) & 0xFF. Each entry waits for the previous byte's last
; ---- rising edge (e<i>), then launches bit 7 (e<i>_l, where the tree jumps in) and shifts the rest.
; ---- Falling through from entry i to i+1 is the auto-increment.
e0:
        JPIN 2 1 idle
        JPIN 0 0 e0
e0_l:
        JPIN 2 1 idle
        JPIN 0 1 e0_l
        PIN  0 0
        LDA  0x06
        LDX  7
        CALL sb
e1:
        JPIN 2 1 idle
        JPIN 0 0 e1
e1_l:
        JPIN 2 1 idle
        JPIN 0 1 e1_l
        PIN  0 0
        LDA  0x14
        LDX  7
        CALL sb
e2:
        JPIN 2 1 idle
        JPIN 0 0 e2
e2_l:
        JPIN 2 1 idle
        JPIN 0 1 e2_l
        PIN  0 0
        LDA  0x22
        LDX  7
        CALL sb
e3:
        JPIN 2 1 idle
        JPIN 0 0 e3
e3_l:
        JPIN 2 1 idle
        JPIN 0 1 e3_l
        PIN  0 0
        LDA  0x30
        LDX  7
        CALL sb
e4:
        JPIN 2 1 idle
        JPIN 0 0 e4
e4_l:
        JPIN 2 1 idle
        JPIN 0 1 e4_l
        PIN  0 0
        LDA  0x3e
        LDX  7
        CALL sb
e5:
        JPIN 2 1 idle
        JPIN 0 0 e5
e5_l:
        JPIN 2 1 idle
        JPIN 0 1 e5_l
        PIN  0 0
        LDA  0x4c
        LDX  7
        CALL sb
e6:
        JPIN 2 1 idle
        JPIN 0 0 e6
e6_l:
        JPIN 2 1 idle
        JPIN 0 1 e6_l
        PIN  0 0
        LDA  0x5a
        LDX  7
        CALL sb
e7:
        JPIN 2 1 idle
        JPIN 0 0 e7
e7_l:
        JPIN 2 1 idle
        JPIN 0 1 e7_l
        PIN  0 0
        LDA  0x68
        LDX  7
        CALL sb
        JMP  e0

; ---- Thread 1: the command counter. Its program counter IS the count: it walks a ring of
; ---- 'LDA n / SEND' pairs, and the one-byte channel always holds the NEXT count value.
; ---- The worker RECVs once per completed command, which lets the ring advance by one.
ring:
        LDA  1
        SEND
        LDA  2
        SEND
        LDA  3
        SEND
        LDA  4
        SEND
        LDA  5
        SEND
        LDA  6
        SEND
        LDA  7
        SEND
        LDA  8
        SEND
        LDA  9
        SEND
        LDA  10
        SEND
        LDA  11
        SEND
        LDA  12
        SEND
        LDA  13
        SEND
        LDA  14
        SEND
        LDA  15
        SEND
        LDA  16
        SEND
        LDA  17
        SEND
        LDA  18
        SEND
        LDA  19
        SEND
        LDA  20
        SEND
        LDA  21
        SEND
        LDA  22
        SEND
        LDA  23
        SEND
        LDA  24
        SEND
        LDA  25
        SEND
        LDA  26
        SEND
        LDA  27
        SEND
        LDA  28
        SEND
        LDA  29
        SEND
        LDA  30
        SEND
        LDA  31
        SEND
        LDA  0
        SEND
        JMP  ring
