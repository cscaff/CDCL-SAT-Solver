"""
Full-stack integration test: JTAG ↔ JTAGHostInterface ↔ BCPAccelerator.

Instantiates BCPTopJTAG with use_jtagg_primitive=False (simulation mode)
and drives the JTAG shift/update interface directly.  Verifies the same
scenarios as test_integration.py but using JTAG communication.

Scenarios (same setup as test_bcp_end_to_end.py Scenario A):
  1. Implication chain: a=T → b=T → c=T → d=T (no conflict)
  2. Conflict scenario
"""

import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from top_jtag import BCPTopJTAG
from communication.jtag_host_interface import (
    CMD_WRITE_CLAUSE, CMD_WRITE_WL_ENTRY, CMD_WRITE_WL_LEN,
    CMD_WRITE_ASSIGN, CMD_BCP_START, CMD_ACK_IMPL,
    RSP_IDLE, RSP_BUSY, RSP_IMPLICATION, RSP_DONE_OK, RSP_DONE_CONF,
    REG_WIDTH,
)


# ── HW assignment encoding ────────────────────────────────────────────────
HW_UNASSIGNED = 0
HW_FALSE      = 1
HW_TRUE       = 2


# ── JTAG helpers (same as unit test) ──────────────────────────────────────

def build_command(cmd_byte, payload_bytes, seq_num):
    val = (cmd_byte & 0xFF) << 120
    for i, b in enumerate(payload_bytes):
        val |= (b & 0xFF) << (112 - i * 8)
    val |= (seq_num & 0xFF)
    return val


class JTAGDriver:
    """Helper to drive JTAG interface in simulation testbench."""

    def __init__(self, dut):
        self.dut = dut
        self.host_if = dut.host_if
        self.seq = 0

    async def send_cmd(self, ctx, cmd_byte, payload_bytes):
        """Send a JTAG command (shift + update). Returns shift-out bits.
        JTAG is LSB-first: bit 0 is shifted in/out first.
        Shift and update tick on the jtck domain."""
        self.seq += 1
        cmd_bits = build_command(cmd_byte, payload_bytes, self.seq)
        response = 0

        # Capture-DR: sel=1, shift=0 for one cycle (pre-loads shift_reg)
        ctx.set(self.host_if.jtag_sel, 1)
        ctx.set(self.host_if.jtag_shift, 0)
        await ctx.tick("jtck")

        # Shift-DR: shift 128 bits in/out
        ctx.set(self.host_if.jtag_shift, 1)
        for i in range(REG_WIDTH):
            ctx.set(self.host_if.jtag_tdi, (cmd_bits >> i) & 1)
            tdo_bit = ctx.get(self.host_if.jtag_tdo)
            response |= (tdo_bit << i)
            await ctx.tick("jtck")

        # Update-DR: latch the shifted-in command
        ctx.set(self.host_if.jtag_shift, 0)
        ctx.set(self.host_if.jtag_update, 1)
        await ctx.tick("jtck")
        ctx.set(self.host_if.jtag_update, 0)
        await ctx.tick("jtck")

        return response

    async def send_and_wait(self, ctx, cmd_byte, payload_bytes, wait=20):
        """Send command and wait for CDC + processing (sync ticks)."""
        rsp = await self.send_cmd(ctx, cmd_byte, payload_bytes)
        for _ in range(wait):
            await ctx.tick("sync")
        return rsp

    async def read_response(self, ctx):
        """
        Read the current response: flush scan + read scan (NOP only).
        Returns (status, var, val, reason_id, ack_seq).
        """
        # Flush scan: loads current rsp_shadow into shift_reg
        await self.send_cmd(ctx, 0x00, [])
        # Read scan: shifts out the response loaded at flush jupdate
        rsp = await self.send_cmd(ctx, 0x00, [])
        return self.decode(rsp)

    @staticmethod
    def decode(rsp_bits):
        status    = (rsp_bits >> 120) & 0xFF
        var       = (rsp_bits >> 104) & 0xFFFF
        val       = (rsp_bits >> 96)  & 0xFF
        reason_id = (rsp_bits >> 80)  & 0xFFFF
        ack_seq   = rsp_bits & 0xFF
        return status, var, val, reason_id, ack_seq


# ── Payload encoding helpers ─────────────────────────────────────────────

def encode_write_clause(clause_id, size, sat, lits):
    payload = []
    payload.append((clause_id >> 8) & 0xFF)
    payload.append(clause_id & 0xFF)
    payload.append(size)
    payload.append(sat)
    for k in range(5):
        lit = lits[k] if k < len(lits) else 0
        payload.append((lit >> 8) & 0xFF)
        payload.append(lit & 0xFF)
    return payload


def encode_write_wl_len(lit, length):
    return [(lit >> 8) & 0xFF, lit & 0xFF, length]


def encode_write_wl_entry(lit, idx, clause_id):
    return [
        (lit >> 8) & 0xFF, lit & 0xFF,
        idx,
        (clause_id >> 8) & 0xFF, clause_id & 0xFF,
    ]


def encode_write_assign(var, val):
    return [(var >> 8) & 0xFF, var & 0xFF, val]


def encode_bcp_start(false_lit):
    return [(false_lit >> 8) & 0xFF, false_lit & 0xFF]


# ── Test: Implication chain (Scenario A) ─────────────────────────────────

def test_integration_jtag_implication_chain():
    """
    Full-stack JTAG test: upload 3 clauses forming a→b→c→d chain,
    start BCP with ¬a as false_lit, and verify implications via JTAG.

    Clauses:
      C0: (¬a ∨ b) → lits [3, 4]
      C1: (¬b ∨ c) → lits [5, 6]
      C2: (¬c ∨ d) → lits [7, 8]

    Watch lists:
      lit 3 (¬a) → [C0]
      lit 5 (¬b) → [C1]
      lit 7 (¬c) → [C2]
    """
    dut = BCPTopJTAG(use_jtagg_primitive=False)
    sim = Simulator(dut)
    sim.add_clock(1e-8)                # 100 MHz system clock (sync)
    sim.add_clock(1.3e-7, domain="jtck")  # ~7.7 MHz JTAG clock

    async def testbench(ctx):
        drv = JTAGDriver(dut)

        # Init JTAG signals
        ctx.set(dut.host_if.jtag_shift, 0)
        ctx.set(dut.host_if.jtag_update, 0)
        ctx.set(dut.host_if.jtag_tdi, 0)
        ctx.set(dut.host_if.jtag_sel, 0)
        for _ in range(10):
            await ctx.tick("sync")

        # ── Upload clauses ────────────────────────────────────────────
        await drv.send_and_wait(ctx, CMD_WRITE_CLAUSE,
                                encode_write_clause(0, 2, 0, [3, 4]))
        await drv.send_and_wait(ctx, CMD_WRITE_CLAUSE,
                                encode_write_clause(1, 2, 0, [5, 6]))
        await drv.send_and_wait(ctx, CMD_WRITE_CLAUSE,
                                encode_write_clause(2, 2, 0, [7, 8]))

        # ── Upload watch lists ────────────────────────────────────────
        await drv.send_and_wait(ctx, CMD_WRITE_WL_LEN,
                                encode_write_wl_len(3, 1))
        await drv.send_and_wait(ctx, CMD_WRITE_WL_ENTRY,
                                encode_write_wl_entry(3, 0, 0))

        await drv.send_and_wait(ctx, CMD_WRITE_WL_LEN,
                                encode_write_wl_len(5, 1))
        await drv.send_and_wait(ctx, CMD_WRITE_WL_ENTRY,
                                encode_write_wl_entry(5, 0, 1))

        await drv.send_and_wait(ctx, CMD_WRITE_WL_LEN,
                                encode_write_wl_len(7, 1))
        await drv.send_and_wait(ctx, CMD_WRITE_WL_ENTRY,
                                encode_write_wl_entry(7, 0, 2))

        # ── Upload assignments ────────────────────────────────────────
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(1, HW_TRUE))
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(2, HW_UNASSIGNED))
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(3, HW_UNASSIGNED))
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(4, HW_UNASSIGNED))

        # ── Round 1: BCP on false_lit=3 (¬a) ─────────────────────────
        await drv.send_and_wait(ctx, CMD_BCP_START,
                                encode_bcp_start(3), wait=80)

        # Read implication: should be b=TRUE (var=2, val=1, reason=0)
        status, var, val, reason_id, _ = await drv.read_response(ctx)
        assert status == RSP_IMPLICATION, \
            f"Expected IMPL (0xB0), got 0x{status:02X}"
        assert var == 2, f"Expected var=2, got {var}"
        assert val == 1, f"Expected val=1, got {val}"
        assert reason_id == 0, f"Expected reason=0, got {reason_id}"
        print(f"  PASS: impl var={var} val={val} reason={reason_id}")

        # Send ACK_IMPL separately, wait for FSM to process
        await drv.send_and_wait(ctx, CMD_ACK_IMPL, [])

        # Read done
        status, _, _, _, _ = await drv.read_response(ctx)
        assert status == RSP_DONE_OK, \
            f"Expected DONE_OK (0xC0), got 0x{status:02X}"
        print("  PASS: round 1 done-no-conflict")

        # ── Round 2: Update b=TRUE, BCP on false_lit=5 (¬b) ─────────
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(2, HW_TRUE))
        await drv.send_and_wait(ctx, CMD_BCP_START,
                                encode_bcp_start(5), wait=80)

        status, var, val, reason_id, _ = await drv.read_response(ctx)
        assert status == RSP_IMPLICATION
        assert var == 3 and val == 1 and reason_id == 1
        print(f"  PASS: impl var={var} val={val} reason={reason_id}")

        await drv.send_and_wait(ctx, CMD_ACK_IMPL, [])

        status, _, _, _, _ = await drv.read_response(ctx)
        assert status == RSP_DONE_OK
        print("  PASS: round 2 done-no-conflict")

        # ── Round 3: Update c=TRUE, BCP on false_lit=7 (¬c) ─────────
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(3, HW_TRUE))
        await drv.send_and_wait(ctx, CMD_BCP_START,
                                encode_bcp_start(7), wait=80)

        status, var, val, reason_id, _ = await drv.read_response(ctx)
        assert status == RSP_IMPLICATION
        assert var == 4 and val == 1 and reason_id == 2
        print(f"  PASS: impl var={var} val={val} reason={reason_id}")

        await drv.send_and_wait(ctx, CMD_ACK_IMPL, [])

        status, _, _, _, _ = await drv.read_response(ctx)
        assert status == RSP_DONE_OK
        print("  PASS: round 3 done-no-conflict (final)")

        print("\n  JTAG Integration test (implication chain): ALL PASSED")

    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__),
                            "integration_jtag_impl_chain.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()


# ── Test: Conflict scenario ──────────────────────────────────────────────

def test_integration_jtag_conflict():
    """
    Full-stack JTAG test: upload clauses that lead to a conflict.

    Clauses:
      C0: (¬e ∨ f)   → lits [11, 12]
      C1: (¬f ∨ ¬g)  → lits [13, 15]

    Watch lists:
      lit 11 (¬e) → [C0]
      lit 13 (¬f) → [C1]

    Initial assignment: var 5 (e) = TRUE, var 7 (g) = TRUE
    BCP trigger: false_lit = 11 (¬e)
    """
    dut = BCPTopJTAG(use_jtagg_primitive=False)
    sim = Simulator(dut)
    sim.add_clock(1e-8)                # 100 MHz system clock (sync)
    sim.add_clock(1.3e-7, domain="jtck")  # ~7.7 MHz JTAG clock

    async def testbench(ctx):
        drv = JTAGDriver(dut)

        ctx.set(dut.host_if.jtag_shift, 0)
        ctx.set(dut.host_if.jtag_update, 0)
        ctx.set(dut.host_if.jtag_tdi, 0)
        ctx.set(dut.host_if.jtag_sel, 0)
        for _ in range(10):
            await ctx.tick("sync")

        # ── Upload clauses ────────────────────────────────────────────
        await drv.send_and_wait(ctx, CMD_WRITE_CLAUSE,
                                encode_write_clause(0, 2, 0, [11, 12]))
        await drv.send_and_wait(ctx, CMD_WRITE_CLAUSE,
                                encode_write_clause(1, 2, 0, [13, 15]))

        # ── Upload watch lists ────────────────────────────────────────
        await drv.send_and_wait(ctx, CMD_WRITE_WL_LEN,
                                encode_write_wl_len(11, 1))
        await drv.send_and_wait(ctx, CMD_WRITE_WL_ENTRY,
                                encode_write_wl_entry(11, 0, 0))

        await drv.send_and_wait(ctx, CMD_WRITE_WL_LEN,
                                encode_write_wl_len(13, 1))
        await drv.send_and_wait(ctx, CMD_WRITE_WL_ENTRY,
                                encode_write_wl_entry(13, 0, 1))

        # ── Upload assignments ────────────────────────────────────────
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(5, HW_TRUE))
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(6, HW_UNASSIGNED))
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(7, HW_TRUE))

        # ── Round 1: BCP on false_lit=11 (¬e) ────────────────────────
        await drv.send_and_wait(ctx, CMD_BCP_START,
                                encode_bcp_start(11), wait=80)

        status, var, val, reason_id, _ = await drv.read_response(ctx)
        assert status == RSP_IMPLICATION
        assert var == 6 and val == 1 and reason_id == 0
        print(f"  PASS: impl var={var} val={val} reason={reason_id}")

        await drv.send_and_wait(ctx, CMD_ACK_IMPL, [])

        status, _, _, _, _ = await drv.read_response(ctx)
        assert status == RSP_DONE_OK
        print("  PASS: round 1 done-no-conflict")

        # ── Round 2: Update f=TRUE, BCP on false_lit=13 (¬f) ────────
        await drv.send_and_wait(ctx, CMD_WRITE_ASSIGN,
                                encode_write_assign(6, HW_TRUE))
        await drv.send_and_wait(ctx, CMD_BCP_START,
                                encode_bcp_start(13), wait=80)

        status, _, _, reason_id, _ = await drv.read_response(ctx)
        assert status == RSP_DONE_CONF, \
            f"Expected DONE_CONF (0xC1), got 0x{status:02X}"
        assert reason_id == 1, \
            f"Expected conflict clause=1, got {reason_id}"
        print(f"  PASS: conflict on clause {reason_id}")

        print("\n  JTAG Integration test (conflict): ALL PASSED")

    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__),
                            "integration_jtag_conflict.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()


if __name__ == "__main__":
    test_integration_jtag_implication_chain()
    test_integration_jtag_conflict()
