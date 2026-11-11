; SPI master, mode 0 (CPOL = 0, CPHA = 0), MSB first, 16-bit frame, 5 MHz SCK: the behaviour of the ROM
; SPI master (docs/info.md) at five times the speed. Loaded into the program memory, it is not in the ROM.
; Build the image for the fast schedule with:
;     python3 tools/load_image.py --sched 0,1,0,2 build/spi_master_fast firmware/examples/spi_master_fast.asm
; Documentation: docs/examples-fast-and-memory.md. Test: test/test_spi_master_fast.py.
;
; Pins (the same as the ROM SPI master):
;   Input pins : 0-7 = ui[7:0] (the byte to send), 8 = uio[0] (START, level-triggered), 9 = uio[1] (MISO)
;   Output pins: 0 = uo[0] CS_N, 1 = uo[1] SCK, 2 = uo[2] MOSI, 3 = uo[3] BUSY
;   Never used : uio[5], uio[4] (the loader's LOAD / STROBE). No open-drain pad is driven.
;
; A transfer: put a byte on ui[7:0], raise START and hold it. The program sends that byte, receives the
; slave's first byte, sends it straight back (so the slave sees [your byte][its own byte]) and then waits
; for START to go low before it is ready for the next transfer (the same handshake as the ROM master).
;
; Speed. Only thread 0 runs. With the schedule 0,1,0,2 (byte 512 = 0x84) it owns two clock slots two clocks
; apart and executes one instruction every 2 clocks (40 ns). One bit is five instructions:
;       PIN 1 1     SCK rises: the slave samples MOSI
;       INB 9       A[0] = MISO (the pad as it was 2 clocks earlier, that is at the rising edge)
;       PIN 1 0     SCK falls: the slave shifts its next MISO bit out
;       SHL         C = next bit to send (A is the shift register for both directions)
;       OUTC 2      MOSI = C
; = 10 clocks = 200 ns = 5 MHz, with SCK high for 4 clocks and low for 6, and MOSI stable for 2 clocks before
; and 8 clocks after each rising edge. A loop counter would be a sixth instruction per bit (4.2 MHz), so
; the 16 bits are written out one after the other. The ROM master needs 12 turns of 4 clocks per bit.
; With the default schedule the same program runs at half the rate (every duration is doubled): 2.5 MHz.
;
; Frame: CS_N falls 3 turns before the first rising edge and rises 3 turns after the last falling edge
; (1 turn = 2 clocks with the fast schedule, 4 with the default one).

spi_start:
        PIN  0 1              ; CS_N idle high
        PIN  1 0              ; SCK idle low
        PIN  3 0              ; not busy
spi_idle:
        JPIN 8 0 spi_idle     ; spin while START is low
        IN                    ; A = byte to send
        PIN  3 1              ; busy
        PIN  0 0              ; assert CS_N
        SHL                   ; C = bit 7
        OUTC 2                ; MOSI = bit 7 (the first rising edge follows at once)
; ---- byte 1: ui[7:0] goes out on MOSI while the slave's first byte comes in on MISO
bit0:
        PIN  1 1              ; SCK rises
        INB  9                ; A[0] = MISO
        PIN  1 0              ; SCK falls
        SHL
        OUTC 2
bit1:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit2:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit3:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit4:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit5:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit6:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit7:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
; ---- byte 2: A now holds the received byte; the same shifts send it back out
bit8:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit9:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit10:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit11:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit12:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit13:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit14:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
bit15:
        PIN  1 1
        INB  9
        PIN  1 0
        WAIT 1                ; CS hold: 3 turns from the last falling edge to CS_N
        PIN  0 1              ; deassert CS_N
        PIN  2 0              ; MOSI low
spi_rel:
        JPIN 8 1 spi_rel      ; wait for START to be released
        PIN  3 0              ; not busy
        JMP  spi_idle
