; JTAG (IEEE 1149.1) master: reads the IDCODE of a device (or chain), then loads BYPASS into the IR and
; measures how many devices are in the chain.
; An example of firmware loaded into the program memory after fabrication; it is NOT in the ROM.
; Build the image with:  python3 tools/load_image.py build/jtag firmware/examples/jtag.asm
;
; Input pins : 0 = ui[0] = TDO
; Output pins: 2 = TCK, 3 = TMS, 4 = TDI (push-pull), 0 = UART TX (115200 baud, 8N1)
; Open-drain : uio[2], uio[3] are scratch lines used by the firmware itself (leave unconnected)
; Do not use : uio[5], uio[4] (program loader; LOAD must be low while this runs)
;
; Sequence:
;   1. TAP reset: 5 TCK with TMS = 1. IDCODE is the default instruction after a reset.
;   2. Run-Test/Idle -> Select-DR -> Capture-DR -> Shift-DR, shift out 32 bits, the last with TMS = 1.
;      TDO bit 0 must be 1 (an IDCODE always starts with 1; a bypass register or a dead TDO gives 0).
;      With a chain of several devices the 32 bits are the IDCODE of the device closest to TDO.
;   3. Select-IR, shift 32 ones (the IR of every device becomes all ones = BYPASS, whatever its length,
;      as long as the IR chain is no longer than 32 bits), Update-IR.
;   4. Capture-DR loads 0 into every bypass bit. Shift a single 1 in on TDI and count TCKs until it comes
;      out on TDO: that is the number of devices (one bypass bit each). Back to Run-Test/Idle.
; UART output, in order (6 bytes): IDCODE bytes 0..3 (byte 0 = bits 0..7, the first ones shifted out),
; status (0 = IDCODE bit 0 was 1, 1 = it was 0), chain length (0xFF = the marker never came back in
; 255 clocks).
;
; TCK: high 7 slots, low at least 9 slots: period >= 16 slots = 1.28 us (<= 781 kHz). TMS and TDI change
; while TCK is low, TDO is sampled a few slots after the TCK falling edge, before the rising edge.
;
; Bit order: as in onewire.asm, bits are shifted in with SHL / INB, so the first bit is in A[7], and the
; UART thread sends A with SHL / OUTC: the bytes come out as a human reads them. The chain length is
; counted in A as a bit-reversed binary number for the same reason (see jt_inc); the status byte is
; written reversed (true 1 = 0x80). uio[3] is used as a one-bit memory for the status.

jt_start:
        SPAWN 1 jt_uart
        LDA  0x1F             ; 1,1,1,1,1: Test-Logic-Reset
        LDX  5
        CALL jt_tms
        LDA  0x02             ; 0,1,0,0: Run-Test/Idle, Select-DR, Capture-DR, Shift-DR (TMS idles 0)
        LDX  4
        CALL jt_tms
        JPIN 0 1 jt_idok      ; TDO is already bit 0 of the IDCODE
        ODRV 3 0              ; remember: bit 0 was 0
jt_idok:
        CALL jt_byte          ; IDCODE bits 0..23
        CALL jt_byte
        CALL jt_byte
        LDX  7
        CALL jt_ticks         ; bits 24..30
        PIN  3 1              ; last bit with TMS = 1: leave Shift-DR
        CALL jt_tick
        SEND
        LDA  0x00             ; status 0 = ok
        JPIN 11 1 jt_stsend
        LDA  0x80             ; status 1 (reversed) = IDCODE bit 0 was 0
jt_stsend:
        SEND

        LDA  0x07             ; 1,1,1,0,0: Update-DR, Select-DR, Select-IR, Capture-IR, Shift-IR
        LDX  5
        CALL jt_tms
        PIN  4 1              ; TDI = 1
        LDX  31
jt_ir:
        CALL jt_tck           ; 31 ones with TMS = 0
        DJNZ jt_ir
        PIN  3 1
        CALL jt_tck           ; the 32nd with TMS = 1: Exit1-IR
        LDA  0x03             ; 1,1,0,0: Update-IR, Select-DR, Capture-DR, Shift-DR
        LDX  4
        CALL jt_tms           ; (leaves TMS = 0)

        PIN  4 1
        CALL jt_tck           ; the marker: one 1 in on TDI
        PIN  4 0
        LDA  0
jt_find:
        CALL jt_inc           ; A = number of clocks since the marker
        JPIN 0 1 jt_found     ; marker reached TDO?
        CALL jt_tck
        JMP  jt_find
jt_found:
        SEND
        LDA  0x03             ; 1,1,0: Exit1-DR, Update-DR, Run-Test/Idle
        LDX  3
        CALL jt_tms
        HALT

; One TCK cycle. jt_tick first shifts the TDO bit into A (SHL / INB); jt_tck does not touch A.
jt_tick:
        SHL
        INB  0
jt_tck:
        PIN  2 1
        WAIT 5
        PIN  2 0
        WAIT 4
        RET

jt_ticks:                     ; X ticks
        CALL jt_tick
        DJNZ jt_ticks
        RET

jt_byte:                      ; shift 8 bits in and give them to the UART thread
        LDX  8
        CALL jt_ticks
        SEND
        RET

jt_tms:                       ; X clocks with the TMS bits of A, LSB first
        SHR
        OUTC 3
        CALL jt_tck
        DJNZ jt_tms
        RET

; A = A + 1 where A holds a bit-reversed count: the count's LSB is A[7], so one step of the carry
; chain is "shift left, look at the bit that falls out". A bit is looked at by putting it on a pad
; (uio[2]) and reading the pad back.
jt_inc:
        LDX  8
jt_ic:
        SHL                   ; C = next count bit, LSB first
        ODRVC 2
        JPIN 10 0 jt_iz       ; a 0 absorbs the carry
        DJNZ jt_ic            ; a 1 becomes 0 (SHL already left a 0 in A[0]); the carry goes on
        JMP  jt_nomark        ; the count wrapped around
jt_iz:
        ODRV 2 1
        NOP
        INB  10               ; the 0 becomes 1
        DJNZ jt_ip
        RET
jt_ip:
        SHL                   ; rotate the remaining bits round unchanged
        ODRVC 2
        INB  10
        DJNZ jt_ip
        RET
jt_nomark:
        LDA  0xFF
        SEND
        HALT

; ---- thread 1: UART transmitter (identical to the one in onewire.asm) ------------------------
jt_uart:
        RECV
        PIN  0 0              ; start bit
        LDX  8
        WAIT 104
jt_ubit:
        SHL
        OUTC 0
        WAIT 104
        DJNZ jt_ubit
        NOP
        PIN  0 1              ; stop bit
        WAIT 104
        JMP  jt_uart
