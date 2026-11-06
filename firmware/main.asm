; Boot dispatcher: pick the protocol from the mode pins, sampled once at boot.
;   uio_in[6] = 1 -> I2C master (EEPROM reader, prints on UART)
;   uio_in[7] = 1 -> SPI master
;   neither       -> UART transmitter
; The input pins pass through a 2-flop synchroniser that is cleared during reset,
; so wait a few cycles before the first sample.

        WAIT 3
        JPIN 14 1 i2c_start_fw
        JPIN 15 1 spi_start
        JMP  uart_start
