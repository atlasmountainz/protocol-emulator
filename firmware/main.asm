; Boot dispatcher: pick the protocol from the MODE pin (input pin 15 = uio_in[7]).
;   MODE = 0 -> UART transmitter      MODE = 1 -> SPI master
; The input pins pass through a 2-flop synchroniser that is cleared during reset,
; so wait a few cycles before the first sample.

        WAIT 3
        JPIN 15 1 spi_start
        JMP  uart_start
