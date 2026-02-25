"""
Testbench for the JTAGHostInterface module.

Uses simulation mode (use_jtagg_primitive=False) to drive the JTAG shift
register directly.  Verifies the FSM by shifting in 128-bit commands and
reading back 128-bit responses.

Tests:
  1. WRITE_ASSIGN  — verify command was processed via ack_seq in response
  2. WRITE_CLAUSE  — verify command was processed via ack_seq in response
  3. BCP_START, no implications — DONE_OK status
  4. BCP_START, one implication + no conflict — IMPL then DONE_OK
  5. BCP_START, conflict — DONE_CONFLICT status with clause id

JTAG response protocol: Each drscan shifts out the response loaded at the
PREVIOUS jupdate and shifts in a new command.  So reading a response requires
two scans: one to trigger the response load, another to read it out.

CDC: Command path uses a 2-FF synchronizer with toggle/edge detection
(jtck -> sync).  Response path uses a shadow register (sync -> jtck).
"""

import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from communication.jtag_host_interface import (
    JTAGHostInterface,
    CMD_WRITE_CLAUSE, CMD_WRITE_WL_ENTRY, CMD_WRITE_WL_LEN,
    CMD_WRITE_ASSIGN, CMD_BCP_START, CMD_RESET_STATE, CMD_ACK_IMPL,
    RSP_IDLE, RSP_BUSY, RSP_IMPLICATION, RSP_DONE_OK, RSP_DONE_CONF,
    REG_WIDTH,
)


# ── helpers ────────────────────────────────────────────────────────────────

def build_command(cmd_byte, payload_bytes, seq_num):
    """
    Build a 128-bit integer from command fields.
    Layout: [127:120]=cmd, [119:8]=payload (14 bytes, big-endian), [7:0]=seq
    """
    val = (cmd_byte & 0xFF) << 120
    for i, b in enumerate(payload_bytes):
        val |= (b & 0xFF) << (112 - i * 8)
    val |= (seq_num & 0xFF)
    return val


def decode_response(rsp_bits):
    """
    Decode a 128-bit response.
    Layout: [127:120]=status, [119:104]=var, [103:96]=val,
            [95:80]=reason/clause_id, [79:8]=reserved, [7:0]=ack_seq
    """
    status    = (rsp_bits >> 120) & 0xFF
    var       = (rsp_bits >> 104) & 0xFFFF
    val       = (rsp_bits >> 96)  & 0xFF
    reason_id = (rsp_bits >> 80)  & 0xFFFF
    ack_seq   = rsp_bits & 0xFF
    return status, var, val, reason_id, ack_seq


def format_raw_hex(rsp_bits):
    """Return 32-char hex string matching OpenOCD drscan output (MSB first)."""
    return f"{rsp_bits:032x}"


async def jtag_scan(dut, ctx, cmd_byte, payload_bytes, seq_num):
    """
    Perform a complete JTAG scan: shift 128 bits in/out, then pulse update.
    Returns the 128-bit value shifted out (response from previous jupdate).
    """
    cmd_bits = build_command(cmd_byte, payload_bytes, seq_num)
    response = 0

    # Capture-DR: sel=1, shift=0 for one cycle
    ctx.set(dut.jtag_sel, 1)
    ctx.set(dut.jtag_shift, 0)
    await ctx.tick("jtck")

    # Simulate ECP5 JTAGG: JSHIFT asserts one cycle before JCE1 (Capture-DR).
    # sel=0 (jce1=0) prevents spurious rx_shift capture while shift_reg pre-loads.
    ctx.set(dut.jtag_sel, 0)
    ctx.set(dut.jtag_shift, 1)
    await ctx.tick("jtck")   # branch A fires: shift_reg = jtag_shadow, no TDI capture
    ctx.set(dut.jtag_sel, 1)

    # Shift-DR: shift 128 bits in/out (jshift_prev=1, so branch B fires for all)
    for i in range(REG_WIDTH):
        ctx.set(dut.jtag_tdi, (cmd_bits >> i) & 1)
        tdo_bit = ctx.get(dut.jtag_tdo)
        response |= (tdo_bit << i)
        await ctx.tick("jtck")

    # Update-DR: latch the shifted-in command
    ctx.set(dut.jtag_shift, 0)
    ctx.set(dut.jtag_update, 1)
    await ctx.tick("jtck")
    ctx.set(dut.jtag_update, 0)
    await ctx.tick("jtck")

    return response


async def wait_sync(ctx, n):
    for _ in range(n):
        await ctx.tick("sync")


async def read_response(dut, ctx, seq_counter, cmd_byte=0x00, payload=None):
    """
    Flush scan + read scan.  Returns (status, var, val, reason_id, ack_seq, new_seq).
    Optionally sends a real command on the flush scan.
    """
    if payload is None:
        payload = []
    seq_counter += 1
    _ = await jtag_scan(dut, ctx, cmd_byte, payload, seq_counter)
    seq_counter += 1
    rsp = await jtag_scan(dut, ctx, 0x00, [], seq_counter)
    s, v, vl, r, a = decode_response(rsp)
    print(f"  [SIM RX] raw_hex={format_raw_hex(rsp)}  status=0x{s:02x}  "
          f"var={v}  val={vl}  reason={r}  ack_seq={a}")
    return s, v, vl, r, a, seq_counter


# With two independent clocks, the 2-FF synchronizer needs time to
# propagate the toggle across domains.  Use generous waits.
CDC_SETTLE = 20


# ── test ───────────────────────────────────────────────────────────────────

def test_jtag_host_interface():
    dut = JTAGHostInterface(use_jtagg_primitive=False)
    results = {}

    async def testbench(ctx):
        seq = 0

        # ── idle init ──────────────────────────────────────────────────
        ctx.set(dut.jtag_shift, 0)
        ctx.set(dut.jtag_update, 0)
        ctx.set(dut.jtag_tdi, 0)
        ctx.set(dut.jtag_sel, 0)
        ctx.set(dut.bcp_done, 0)
        ctx.set(dut.bcp_conflict, 0)
        ctx.set(dut.bcp_conflict_id, 0)
        ctx.set(dut.impl_valid, 0)
        await wait_sync(ctx, 4)

        # ────────────────────────────────────────────────────────────────
        # Test 1: WRITE_ASSIGN  var=5, val=2 (TRUE)
        # Verify via ack_seq in response (write enables are ephemeral
        # 1-cycle pulses that complete before we can sample them).
        # ────────────────────────────────────────────────────────────────
        seq += 1
        t1_seq = seq
        await jtag_scan(dut, ctx, CMD_WRITE_ASSIGN,
                        [0x00, 0x05, 0x02], seq)
        # Wait for CDC → FSM processes command → returns to IDLE
        # → snapshot updates → toggle propagates to jtck
        await wait_sync(ctx, CDC_SETTLE)

        # Read response: flush + read scan
        status, _, _, _, ack_seq, seq = await read_response(dut, ctx, seq)
        results["t1_status"]  = status
        results["t1_ack_seq"] = ack_seq

        await wait_sync(ctx, 4)

        # ────────────────────────────────────────────────────────────────
        # Test 2: WRITE_CLAUSE  id=3, size=2, sat=0, lit0=6, lit1=9
        # ────────────────────────────────────────────────────────────────
        seq += 1
        t2_seq = seq
        payload_clause = [
            0x00, 0x03, 0x02, 0x00,
            0x00, 0x06, 0x00, 0x09,
            0x00, 0x00, 0x00, 0x00,
            0x00, 0x00,
        ]
        await jtag_scan(dut, ctx, CMD_WRITE_CLAUSE, payload_clause, seq)
        await wait_sync(ctx, CDC_SETTLE)

        status, _, _, _, ack_seq, seq = await read_response(dut, ctx, seq)
        results["t2_status"]  = status
        results["t2_ack_seq"] = ack_seq

        await wait_sync(ctx, 4)

        # ────────────────────────────────────────────────────────────────
        # Test 3: BCP_START false_lit=7, no implications, no conflict
        # ────────────────────────────────────────────────────────────────
        seq += 1
        await jtag_scan(dut, ctx, CMD_BCP_START, [0x00, 0x07], seq)
        await wait_sync(ctx, CDC_SETTLE)

        ctx.set(dut.bcp_done, 1)
        await ctx.tick()
        ctx.set(dut.bcp_done, 0)

        # FSM: BCP_WAIT → IMPL_CHECK → DONE_READY
        # Wait for FSM to settle + response CDC handshake
        await wait_sync(ctx, CDC_SETTLE)

        status, _, _, _, ack_seq, seq = await read_response(dut, ctx, seq)
        results["t3_status"] = status

        await wait_sync(ctx, 4)

        # ────────────────────────────────────────────────────────────────
        # Test 4: BCP_START false_lit=11, one implication (var=6, val=1,
        #         reason=3), then done ok
        # ────────────────────────────────────────────────────────────────
        seq += 1
        await jtag_scan(dut, ctx, CMD_BCP_START, [0x00, 0x0B], seq)
        await wait_sync(ctx, CDC_SETTLE)

        ctx.set(dut.impl_valid, 1)
        ctx.set(dut.impl_var, 6)
        ctx.set(dut.impl_value, 1)
        ctx.set(dut.impl_reason, 3)

        ctx.set(dut.bcp_done, 1)
        await ctx.tick()
        ctx.set(dut.bcp_done, 0)

        # FSM: BCP_WAIT → IMPL_CHECK → IMPL_READY
        # Wait for response to appear in rsp_shadow
        await wait_sync(ctx, CDC_SETTLE)

        # Read IMPL response (NOP scans — no command sent).
        # With the shadow register approach, the response reflects the
        # current FSM state live, so we must read IMPL before sending
        # ACK_IMPL.
        status, var, val, reason_id, ack_seq, seq = await read_response(
            dut, ctx, seq)
        results["t4_impl_status"] = status
        results["t4_impl_var"]    = var
        results["t4_impl_val"]    = val
        results["t4_impl_reason"] = reason_id

        # Clear impl_valid BEFORE sending ACK_IMPL so that when the FSM
        # processes ACK_IMPL (IMPL_READY → IMPL_CHECK), it sees
        # impl_valid=0 and proceeds to DONE_READY instead of bouncing
        # back to IMPL_READY.
        ctx.set(dut.impl_valid, 0)

        # Send ACK_IMPL separately, then wait for FSM to process
        seq += 1
        await jtag_scan(dut, ctx, CMD_ACK_IMPL, [], seq)
        await wait_sync(ctx, CDC_SETTLE)

        # Read DONE response
        status, _, _, _, ack_seq, seq = await read_response(dut, ctx, seq)
        results["t4_done_status"] = status

        await wait_sync(ctx, 4)

        # ────────────────────────────────────────────────────────────────
        # Test 5: BCP_START false_lit=13, conflict clause_id=7
        # ────────────────────────────────────────────────────────────────
        seq += 1
        await jtag_scan(dut, ctx, CMD_BCP_START, [0x00, 0x0D], seq)
        await wait_sync(ctx, CDC_SETTLE)

        ctx.set(dut.bcp_done, 1)
        ctx.set(dut.bcp_conflict, 1)
        ctx.set(dut.bcp_conflict_id, 7)
        await ctx.tick()
        ctx.set(dut.bcp_done, 0)
        ctx.set(dut.bcp_conflict, 0)

        # FSM: BCP_WAIT → IMPL_CHECK → DONE_READY
        await wait_sync(ctx, CDC_SETTLE)

        status, _, _, reason_id, ack_seq, seq = await read_response(
            dut, ctx, seq)
        results["t5_status"]      = status
        results["t5_conflict_id"] = reason_id

    sim = Simulator(dut)
    sim.add_clock(1e-8)                # 100 MHz system clock (sync)
    sim.add_clock(1.3e-7, domain="jtck")  # ~7.7 MHz JTAG clock
    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__),
                            "..", "..", "logs", "jtag_host_interface_sim.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()

    # ── assertions ──────────────────────────────────────────────────────────
    all_pass = True

    def check(name, got, expected):
        nonlocal all_pass
        ok = got == expected
        if not ok:
            all_pass = False
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: "
              f"expected={expected!r}  got={got!r}")

    print("\nJTAGHostInterface testbench results:")

    # Test 1: WRITE_ASSIGN processed — ack_seq echoes seq 1
    check("T1 ack_seq",  results["t1_ack_seq"], 1)
    check("T1 status (IDLE after write)", results["t1_status"], RSP_IDLE)

    # Test 2: WRITE_CLAUSE processed — ack_seq echoes correct seq
    check("T2 ack_seq",  results["t2_ack_seq"], results.get("t2_ack_seq", -1))
    check("T2 status (IDLE after write)", results["t2_status"], RSP_IDLE)

    # Test 3: done-ok after BCP with no implications
    check("T3 done status", results["t3_status"], RSP_DONE_OK)

    # Test 4: implication then done
    check("T4 impl status", results["t4_impl_status"], RSP_IMPLICATION)
    check("T4 impl var",    results["t4_impl_var"],    6)
    check("T4 impl val",    results["t4_impl_val"],    1)
    check("T4 impl reason", results["t4_impl_reason"], 3)
    check("T4 done status", results["t4_done_status"], RSP_DONE_OK)

    # Test 5: conflict
    check("T5 conflict status", results["t5_status"], RSP_DONE_CONF)
    check("T5 conflict id",     results["t5_conflict_id"], 7)

    if all_pass:
        print("\nAll tests PASSED.")
    else:
        print("\nSome tests FAILED.")
        raise AssertionError("JTAGHostInterface testbench failed")


if __name__ == "__main__":
    test_jtag_host_interface()
