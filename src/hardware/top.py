"""
BCP Accelerator Top-Level Module.

Wires together the four main submodules and connects them to FPGA I/O:

    Platform UART pins
        │
        ├─ rx_pin ──► UARTReceiver ──► HostInterface
        │                                    │
        │                             BCPAccelerator
        │                                    │
        └─ tx_pin ◄── UARTTransmitter ◄──────┘

Build target: Lattice ECP5-5G Evaluation Board (12 MHz system clock).
Baud rate: 1 Mbaud → divisor = 12.
"""

from amaranth import *

from communication.uart_rx import UARTReceiver
from communication.uart_tx import UARTTransmitter
from communication.host_interface import HostInterface
from modules.bcp_accelerator import BCPAccelerator


class BCPTop(Elaboratable):
    def __init__(self):
        self.uart_rx = UARTReceiver(divisor=12)
        self.uart_tx = UARTTransmitter(divisor=12)
        self.host_if = HostInterface()
        self.bcp     = BCPAccelerator()

    def elaborate(self, platform):
        m = Module()

        # ── Instantiate submodules ───────────────────────────────────────
        uart_rx = self.uart_rx
        uart_tx = self.uart_tx
        host_if = self.host_if
        bcp     = self.bcp

        m.submodules.uart_rx = uart_rx
        m.submodules.uart_tx = uart_tx
        m.submodules.host_if = host_if
        m.submodules.bcp     = bcp

        # ── Platform I/O ─────────────────────────────────────────────────
        if platform is not None:
            uart = platform.request("uart", 0)
            m.d.comb += [
                uart_rx.rx_pin.eq(uart.rx.i),
                uart.tx.o.eq(uart_tx.tx_pin),
            ]

            # Optional LED heartbeat — blink LED 0 with a ~1 Hz toggle
            # at 12 MHz: bit 22 of a 23-bit counter toggles at ~1.4 Hz
            led = platform.request("led", 0)
            heartbeat = Signal(23)
            m.d.sync += heartbeat.eq(heartbeat + 1)
            m.d.comb += led.o.eq(heartbeat[-1])

        # ── UART RX → HostInterface ─────────────────────────────────────
        m.d.comb += [
            host_if.rx_data.eq(uart_rx.rx_data),
            host_if.rx_valid.eq(uart_rx.rx_valid),
        ]

        # ── HostInterface → UART TX ─────────────────────────────────────
        m.d.comb += [
            uart_tx.tx_data.eq(host_if.tx_data),
            uart_tx.tx_valid.eq(host_if.tx_valid),
            host_if.tx_ready.eq(uart_tx.tx_ready),
        ]

        # ── HostInterface → BCP control ─────────────────────────────────
        m.d.comb += [
            bcp.start.eq(host_if.bcp_start),
            bcp.false_lit.eq(host_if.bcp_false_lit),
        ]

        # ── BCP → HostInterface (done / conflict) ───────────────────────
        m.d.comb += [
            host_if.bcp_done.eq(bcp.done),
            host_if.bcp_conflict.eq(bcp.conflict),
            host_if.bcp_conflict_id.eq(bcp.conflict_clause_id),
        ]

        # ── BCP → HostInterface (implication stream) ────────────────────
        m.d.comb += [
            host_if.impl_valid.eq(bcp.impl_valid),
            host_if.impl_var.eq(bcp.impl_var),
            host_if.impl_value.eq(bcp.impl_value),
            host_if.impl_reason.eq(bcp.impl_reason),
            bcp.impl_ready.eq(host_if.impl_ready),
        ]

        # ── HostInterface → BCP write ports (clause database) ───────────
        m.d.comb += [
            bcp.clause_wr_addr.eq(host_if.clause_wr_addr),
            bcp.clause_wr_sat_bit.eq(host_if.clause_wr_sat_bit),
            bcp.clause_wr_size.eq(host_if.clause_wr_size),
            bcp.clause_wr_lit0.eq(host_if.clause_wr_lit0),
            bcp.clause_wr_lit1.eq(host_if.clause_wr_lit1),
            bcp.clause_wr_lit2.eq(host_if.clause_wr_lit2),
            bcp.clause_wr_lit3.eq(host_if.clause_wr_lit3),
            bcp.clause_wr_lit4.eq(host_if.clause_wr_lit4),
            bcp.clause_wr_en.eq(host_if.clause_wr_en),
        ]

        # ── HostInterface → BCP write ports (watch lists) ───────────────
        m.d.comb += [
            bcp.wl_wr_lit.eq(host_if.wl_wr_lit),
            bcp.wl_wr_idx.eq(host_if.wl_wr_idx),
            bcp.wl_wr_data.eq(host_if.wl_wr_data),
            bcp.wl_wr_len.eq(host_if.wl_wr_len),
            bcp.wl_wr_en.eq(host_if.wl_wr_en),
            bcp.wl_wr_len_en.eq(host_if.wl_wr_len_en),
        ]

        # ── HostInterface → BCP write ports (assignments) ───────────────
        m.d.comb += [
            bcp.assign_wr_addr.eq(host_if.assign_wr_addr),
            bcp.assign_wr_data.eq(host_if.assign_wr_data),
            bcp.assign_wr_en.eq(host_if.assign_wr_en),
        ]

        return m


if __name__ == "__main__":
    from amaranth_boards.ecp5_5g_evn import ECP55GEVNPlatform
    platform = ECP55GEVNPlatform()
    platform.build(BCPTop(), do_program=False, name="bcp_accel")
