"""
BCP Accelerator Top-Level Module — JTAG Communication.

Wires together the JTAGHostInterface and BCPAccelerator.  Uses the ECP5
JTAGG primitive for host communication instead of UART.

    JTAG (via JTAGG primitive)
        │
        └─ JTAGHostInterface ─── BCPAccelerator

Build target: Lattice ECP5-5G Evaluation Board (12 MHz system clock).
"""

from amaranth import *

from communication.jtag_host_interface import JTAGHostInterface
from modules.bcp_accelerator import BCPAccelerator


class BCPTopJTAG(Elaboratable):
    def __init__(self, use_jtagg_primitive=True, diagnostic_mode=False):
        self.host_if = JTAGHostInterface(
            use_jtagg_primitive=use_jtagg_primitive,
            diagnostic_mode=diagnostic_mode,
        )
        self.bcp = BCPAccelerator()

    def elaborate(self, platform):
        m = Module()

        host_if = self.host_if
        bcp     = self.bcp

        m.submodules.host_if = host_if
        m.submodules.bcp     = bcp

        # ── JTAGHostInterface → BCP control ──────────────────────────────
        m.d.comb += [
            bcp.start.eq(host_if.bcp_start),
            bcp.false_lit.eq(host_if.bcp_false_lit),
        ]

        # ── BCP → JTAGHostInterface (done / conflict) ───────────────────
        m.d.comb += [
            host_if.bcp_done.eq(bcp.done),
            host_if.bcp_conflict.eq(bcp.conflict),
            host_if.bcp_conflict_id.eq(bcp.conflict_clause_id),
        ]

        # ── BCP → JTAGHostInterface (implication stream) ────────────────
        m.d.comb += [
            host_if.impl_valid.eq(bcp.impl_valid),
            host_if.impl_var.eq(bcp.impl_var),
            host_if.impl_value.eq(bcp.impl_value),
            host_if.impl_reason.eq(bcp.impl_reason),
            bcp.impl_ready.eq(host_if.impl_ready),
        ]

        # ── JTAGHostInterface → BCP write ports (clause database) ───────
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

        # ── JTAGHostInterface → BCP write ports (watch lists) ───────────
        m.d.comb += [
            bcp.wl_wr_lit.eq(host_if.wl_wr_lit),
            bcp.wl_wr_idx.eq(host_if.wl_wr_idx),
            bcp.wl_wr_data.eq(host_if.wl_wr_data),
            bcp.wl_wr_len.eq(host_if.wl_wr_len),
            bcp.wl_wr_en.eq(host_if.wl_wr_en),
            bcp.wl_wr_len_en.eq(host_if.wl_wr_len_en),
        ]

        # ── JTAGHostInterface → BCP write ports (assignments) ───────────
        m.d.comb += [
            bcp.assign_wr_addr.eq(host_if.assign_wr_addr),
            bcp.assign_wr_data.eq(host_if.assign_wr_data),
            bcp.assign_wr_en.eq(host_if.assign_wr_en),
        ]

        return m


if __name__ == "__main__":
    from amaranth_boards.ecp5_5g_evn import ECP55GEVNPlatform
    platform = ECP55GEVNPlatform()
    platform.build(BCPTopJTAG(), do_program=False, name="bcp_accel_jtag")
