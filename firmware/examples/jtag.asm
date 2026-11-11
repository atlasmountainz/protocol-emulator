; JTAG (IEEE 1149.1) master: reads the IDCODE of a device (or chain), then loads BYPASS into the IR and
; measures how many devices are in the chain.
; An example of firmware loaded into the program memory after fabrication; it is NOT in the ROM.
; Build the image with:  python3 tools/load_image.py build/jtag firmware/examples/jtag.asm
;
; Input pins : 0 = ui[0] = TDO
; Output pins: 2 = TCK, 3 = TMS, 4 = TDI (push-pull), 0 = UART TX (115200 baud, 8N1)
; Open-drain : none (uio_oe stays 0)
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
; TCK: high 7 turns, low at least 11 turns: period >= 18 turns = 1.44 us (<= 694 kHz). TMS and TDI change
; while TCK is low, TDO is sampled at least 10 turns after the TCK falling edge, before the rising edge.
;
; How it is done: the TMS paths between the scans are TMS bit patterns in A, first bit in A[0], with a
; 1 above the last bit as an end marker (jt_tms). The two 32-bit scans count their TCKs in X; the last
; one gets TMS = 1 because XCMP 2 sets C = (X < 2). A received bit is shifted into the top of A (SHR,
; then OR 0x80 if TDO is 1), and a 0x80 in A at the start of every byte falls out into C after 8 bits,
; which ends the byte; the first bit shifted in is the LSB, so A holds the true byte and goes to the UART as
; it is. The chain length is a plain binary count in A (INC). The status is kept in MEM[0] (data memory
; is zero after reset, which the program relies on).

jt_start:
        SPAWN 1 jt_uart
        LDA  0x3F             ; TMS 1,1,1,1,1: Test-Logic-Reset
        CALL jt_tms
        LDA  0x12             ; TMS 0,1,0,0: Run-Test/Idle, Select-DR, Capture-DR, Shift-DR
        CALL jt_tms
        JPIN 0 1 jt_idok      ; TDO is already bit 0 of the IDCODE
        LDA  1
        STM  0                ; status 1: bit 0 was 0
jt_idok:
        LDX  32               ; 32 TCKs, the last one with TMS = 1
jt_idbyte:
        LDA  0x80             ; byte register with its end marker
jt_idbit:
        XCMP 2                ; C = 1 on the last TCK
        OUTC 3                ; TMS = C
        SHR                   ; C = marker bit; make room at the top
        JPIN 0 1 jt_one       ; TDO is the bit to shift in
        JMP  jt_join          ; a 0: nothing to insert (both paths take one turn)
jt_one:
        OR   0x80             ; a 1: becomes the new top bit
jt_join:
        CALL jt_tck
        JC   jt_idsend        ; the marker came out: A holds the complete byte
        DJNZ jt_idbit
jt_idsend:
        SEND
        DJNZ jt_idbyte
        LDM  0
        SEND                  ; status

        LDA  0x27             ; TMS 1,1,1,0,0: Update-DR, Select-DR, Select-IR, Capture-IR, Shift-IR
        CALL jt_tms
        PIN  4 1              ; TDI = 1
        LDX  32               ; 32 ones, the last TCK with TMS = 1: Exit1-IR
jt_ir:
        XCMP 2
        OUTC 3
        CALL jt_tck
        DJNZ jt_ir
        LDA  0x13             ; TMS 1,1,0,0: Update-IR, Select-DR, Capture-DR, Shift-DR (leaves TMS = 0)
        CALL jt_tms           ; (and A = 0)

        CALL jt_tck           ; the marker: one 1 in on TDI
        PIN  4 0
jt_find:
        INC                   ; A = number of clocks since the marker
        JZ   jt_nomark        ; 256: it never came back
        JPIN 0 1 jt_found     ; marker reached TDO?
        CALL jt_tck
        JMP  jt_find
jt_found:
        SEND
        LDA  0x0B             ; TMS 1,1,0: Exit1-DR, Update-DR, Run-Test/Idle
        CALL jt_tms
        HALT
jt_nomark:
        NOT                   ; A = 0: report 0xFF
        SEND
        HALT

jt_tck:                       ; one TCK cycle: high 7 turns, then low
        PIN  2 1
        WAIT 5
        PIN  2 0
        WAIT 4
        RET

jt_tms:                       ; one TCK per bit of A, LSB first, until only the end marker is left (A = 0)
        SHR
        JZ   jt_tmsx          ; the end marker came out
        OUTC 3
        CALL jt_tck
        JMP  jt_tms
jt_tmsx:
        RET

; ---- thread 1: UART transmitter (identical to the one in onewire.asm) ------------------------
jt_uart:
        RECV
        PIN  0 0              ; start bit
        LDX  8
        WAIT 104
jt_ubit:
        SHR
        OUTC 0
        WAIT 104
        DJNZ jt_ubit
        NOP
        PIN  0 1              ; stop bit
        WAIT 104
        JMP  jt_uart
