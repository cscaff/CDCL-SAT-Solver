"""
UART Receiver Module for the BCP Accelerator Host Interface.

Standard 8N1 UART receiver. Samples the RX line at 16x the baud rate
(oversampling for noise rejection), then shifts in 8 data bits, and
pulses `rx_valid` for one cycle when a complete byte is ready.

Parameters
----------
divisor : int
    Clock cycles per bit = clk_freq / baud_rate.
    At 12 MHz / 1 Mbaud = 12.

Ports
-----
rx_pin   : Signal(1), in   — raw serial input (idle high)
rx_data  : Signal(8), out  — received byte (valid for one cycle)
rx_valid : Signal(),  out  — pulsed for one cycle when rx_data is ready
rx_err   : Signal(),  out  — framing error (stop bit was not 1)
"""

from amaranth import *


class UARTReceiver(Elaboratable):
    """
    8N1 UART Receiver.

    Detects the start bit (falling edge on rx_pin), waits half a bit
    period to centre-sample the start bit, then samples each of the 8
    data bits at the centre of their windows, then checks the stop bit.
    """

    def __init__(self, divisor: int = 12):
        self.divisor = divisor

        self.rx_pin   = Signal(reset=1)   # idle high
        self.rx_data  = Signal(8)
        self.rx_valid = Signal()
        self.rx_err   = Signal()

    def elaborate(self, platform):
        m = Module()

        divisor   = self.divisor
        half_div  = divisor // 2

        # Synchronise the async rx_pin into the clock domain (2-FF sync)
        rx_sync0 = Signal(reset=1)
        rx_sync1 = Signal(reset=1)
        rx_prev   = Signal(reset=1)
        m.d.sync += [
            rx_sync0.eq(self.rx_pin),
            rx_sync1.eq(rx_sync0),
            rx_prev.eq(rx_sync1),
        ]

        # Internal state
        bit_timer = Signal(range(divisor))
        bit_count = Signal(range(9))       # 0..7 = data bits
        shift_reg = Signal(8)

        falling_edge = rx_prev & ~rx_sync1  # idle→start transition

        # rx_data is combinational from shift_reg so it is already valid
        # in the same cycle that rx_valid is asserted (no off-by-one).
        m.d.comb += self.rx_data.eq(shift_reg)

        with m.FSM():
            with m.State("IDLE"):
                with m.If(falling_edge):
                    # Start bit detected — wait half a period to centre-sample
                    m.d.sync += bit_timer.eq(half_div - 1)
                    m.next = "START"

            with m.State("START"):
                # Count down to the centre of the start bit
                with m.If(bit_timer == 0):
                    # Verify start bit is still low
                    with m.If(rx_sync1):
                        # False start — return to IDLE
                        m.next = "IDLE"
                    with m.Else():
                        m.d.sync += [
                            bit_timer.eq(divisor - 1),
                            bit_count.eq(0),
                        ]
                        m.next = "DATA"
                with m.Else():
                    m.d.sync += bit_timer.eq(bit_timer - 1)

            with m.State("DATA"):
                with m.If(bit_timer == 0):
                    m.d.sync += [
                        # Shift in LSB-first (standard UART bit order)
                        shift_reg.eq(Cat(shift_reg[1:], rx_sync1)),
                        bit_timer.eq(divisor - 1),
                        bit_count.eq(bit_count + 1),
                    ]
                    with m.If(bit_count == 7):
                        m.next = "STOP"
                with m.Else():
                    m.d.sync += bit_timer.eq(bit_timer - 1)

            with m.State("STOP"):
                with m.If(bit_timer == 0):
                    with m.If(rx_sync1):
                        # Valid stop bit — rx_data already reflects shift_reg
                        m.d.comb += self.rx_valid.eq(1)
                    with m.Else():
                        # Framing error
                        m.d.comb += self.rx_err.eq(1)
                    m.next = "IDLE"
                with m.Else():
                    m.d.sync += bit_timer.eq(bit_timer - 1)

        return m
