"""
Testbench for the HostInterface module.

Drives rx_data/rx_valid directly (no UART RX module) and captures
tx_data/tx_valid output (no UART TX module).  Mocks the BCP accelerator
side via bcp_done, impl_valid, impl_var, impl_value, impl_reason.

Tests:
  1. WRITE_ASSIGN  — correct assign_wr_en pulse with right addr/data
  2. WRITE_CLAUSE  — correct clause_wr_en pulse with right addr and lits
  3. BCP_START, no implications — 4-byte done-ok packet (0xC0)
  4. BCP_START, one implication + no conflict — 6-byte impl packet then done-ok
  5. BCP_START, conflict — 4-byte done-conflict packet (0xC1) with clause id
"""

import sys, os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from communication.host_interface import (
    HostInterface,
    CMD_WRITE_CLAUSE, CMD_WRITE_WL_ENTRY, CMD_WRITE_WL_LEN,
    CMD_WRITE_ASSIGN, CMD_BCP_START, CMD_RESET_STATE,
    RSP_IMPLICATION, RSP_DONE_OK, RSP_DONE_CONF,
)


# ── helpers ────────────────────────────────────────────────────────────────
async def send_byte(dut, ctx, byte_val, cycle_cnt):
    """Deliver one byte via the rx_data/rx_valid strobe interface."""
    ctx.set(dut.rx_data,  byte_val)
    ctx.set(dut.rx_valid, 1)
    await ctx.tick()
    cycle_cnt += 1
    ctx.set(dut.rx_valid, 0)
    await ctx.tick()
    cycle_cnt += 1

    return cycle_cnt


async def send_cmd(dut, ctx, cmd, payload_bytes, cycle_cnt):
    """Send command byte followed by payload bytes."""
    cycle_cnt = await send_byte(dut, ctx, cmd, cycle_cnt)
    for b in payload_bytes:
        cycle_cnt = await send_byte(dut, ctx, b, cycle_cnt)

    return cycle_cnt


async def collect_tx_bytes(dut, ctx, n, cycle_cnt, max_cycles=200,):
    """
    Collect n bytes from the TX stream.
    Holds tx_ready=1 so every valid byte is accepted immediately.
    After collecting, does one extra tick to let the FSM commit its
    state transition (m.next is combinational and depends on tx_ready).
    """
    ctx.set(dut.tx_ready, 1)
    collected = []
    cycles = 0
    while len(collected) < n and cycles < max_cycles:
        await ctx.tick()
        cycles += 1
        cycle_cnt += 1
        if ctx.get(dut.tx_valid):
            collected.append(ctx.get(dut.tx_data))
    # One extra tick so the FSM's pending state transition commits
    # before we deassert tx_ready (m.next is combinational).
    await ctx.tick()
    ctx.set(dut.tx_ready, 0)
    return collected


# ── test ───────────────────────────────────────────────────────────────────

def test_host_interface():
    dut = HostInterface()
    results = {}

    async def testbench(ctx):
        cycle_cnt = 0

        # ── idle init ─────────────────────────────────────────────────
        ctx.set(dut.tx_ready,   0)
        ctx.set(dut.rx_valid,   0)
        ctx.set(dut.bcp_done,   0)
        ctx.set(dut.bcp_conflict,    0)
        ctx.set(dut.bcp_conflict_id, 0)
        ctx.set(dut.impl_valid, 0)
        for _ in range(4):
            await ctx.tick()
            cycle_cnt += 1

        # ──────────────────────────────────────────────────────────────
        # Test 1: WRITE_ASSIGN  var=5, val=2 (TRUE)
        # payload: [0x00, 0x05, 0x02]
        # ──────────────────────────────────────────────────────────────

        # Send cmd byte + first two payload bytes normally
        cycle_cnt = await send_byte(dut, ctx, CMD_WRITE_ASSIGN, cycle_cnt)
        cycle_cnt = await send_byte(dut, ctx, 0x00, cycle_cnt)
        cycle_cnt = await send_byte(dut, ctx, 0x05, cycle_cnt)

        # Last payload byte: only do the first tick so we can sample
        # during CMD_EXEC before it transitions to CMD_WAIT.
        ctx.set(dut.rx_data, 0x02)
        ctx.set(dut.rx_valid, 1)
        await ctx.tick(); cycle_cnt += 1
        ctx.set(dut.rx_valid, 0)

        # FSM just entered CMD_EXEC — combinational write enables are active
        results["t1_en"]   = (cycle_cnt, ctx.get(dut.assign_wr_en))
        results["t1_addr"] = (cycle_cnt, ctx.get(dut.assign_wr_addr))
        results["t1_data"] = (cycle_cnt, ctx.get(dut.assign_wr_data))


        # Complete the gap tick to transition out of CMD_EXEC
        await ctx.tick(); cycle_cnt += 1

        # ──────────────────────────────────────────────────────────────
        # Test 2: WRITE_CLAUSE  id=3, size=2, sat=0, lit0=6, lit1=9
        # payload (14 bytes big-endian):
        #   clause_id  [0x00,0x03]
        #   size       [0x02]
        #   sat        [0x00]
        #   lit0       [0x00,0x06]
        #   lit1       [0x00,0x09]
        #   lit2..4    [0x00,0x00]*3
        # ──────────────────────────────────────────────────────────────
        payload_clause = [
            0x00, 0x03,   # clause_id = 3
            0x02,         # size = 2
            0x00,         # sat = 0
            0x00, 0x06,   # lit0 = 6
            0x00, 0x09,   # lit1 = 9
            0x00, 0x00,   # lit2 = 0
            0x00, 0x00,   # lit3 = 0
            0x00, 0x00,   # lit4 = 0
        ]
        # Send cmd byte + first 13 payload bytes normally
        cycle_cnt = await send_byte(dut, ctx, CMD_WRITE_CLAUSE, cycle_cnt)
        for b in payload_clause[:-1]:
            cycle_cnt = await send_byte(dut, ctx, b, cycle_cnt)

        # Last payload byte: only first tick, then sample during CMD_EXEC
        ctx.set(dut.rx_data, payload_clause[-1])
        ctx.set(dut.rx_valid, 1)
        await ctx.tick(); cycle_cnt += 1
        ctx.set(dut.rx_valid, 0)

        # FSM just entered CMD_EXEC — combinational write enables are active
        results["t2_en"]   = (cycle_cnt, ctx.get(dut.clause_wr_en))
        results["t2_addr"] = (cycle_cnt, ctx.get(dut.clause_wr_addr))
        results["t2_lit0"] = (cycle_cnt, ctx.get(dut.clause_wr_lit0))
        results["t2_lit1"] = (cycle_cnt, ctx.get(dut.clause_wr_lit1))

        # Complete the gap tick
        await ctx.tick();   cycle_cnt += 1

        # ──────────────────────────────────────────────────────────────
        # Test 3: BCP_START false_lit=7, no implications, no conflict
        # Expected TX output: [0xC0, 0x00, 0x00, 0x00]
        # ──────────────────────────────────────────────────────────────
        await send_cmd(dut, ctx, CMD_BCP_START, [0x00, 0x07], cycle_cnt)

        # Wait a couple cycles then fire bcp_done
        for _ in range(3):
            await ctx.tick(); cycle_cnt += 1
        ctx.set(dut.bcp_done, 1)
        await ctx.tick(); cycle_cnt += 1
        ctx.set(dut.bcp_done, 0)

        # FSM now in IMPL_CHECK: impl_valid=0 → go to DONE_SEND
        # Collect 4 TX bytes
        results["t3_tx"] = (cycle_cnt, await collect_tx_bytes(dut, ctx, 4, cycle_cnt))

        # ──────────────────────────────────────────────────────────────
        # Test 4: BCP_START false_lit=11, one implication (var=6, val=1
        #         reason=3), then done ok
        # Expected TX: [0xB0, 0x00, 0x06, 0x01, 0x00, 0x03,
        #               0xC0, 0x00, 0x00, 0x00]
        # ──────────────────────────────────────────────────────────────
        for _ in range(4):
            await ctx.tick(); cycle_cnt += 1

        await send_cmd(dut, ctx, CMD_BCP_START, [0x00, 0x0B], cycle_cnt)


        for _ in range(3):
            await ctx.tick(); cycle_cnt += 1

        # Present one implication in the FIFO BEFORE bcp_done fires,
        # so that IMPL_CHECK sees impl_valid=1 when it enters that state.
        ctx.set(dut.impl_valid,  1)
        ctx.set(dut.impl_var,    6)
        ctx.set(dut.impl_value,  1)
        ctx.set(dut.impl_reason, 3)

        ctx.set(dut.bcp_done,   1)
        await ctx.tick();   cycle_cnt += 1
        ctx.set(dut.bcp_done,   0)

        # Collect 6 implication bytes; when impl_ready fires, clear FIFO
        t4_bytes = []
        ctx.set(dut.tx_ready, 1)
        for _ in range(200):
            await ctx.tick(); cycle_cnt += 1
            if ctx.get(dut.tx_valid):
                t4_bytes.append(ctx.get(dut.tx_data))
            if ctx.get(dut.impl_ready):
                # FIFO head popped — no more implications
                ctx.set(dut.impl_valid, 0)
            if len(t4_bytes) == 6:
                break
        ctx.set(dut.tx_ready, 0)

        # Now collect 4 done bytes
        for _ in range(4):
            await ctx.tick(); cycle_cnt += 1
        more = await collect_tx_bytes(dut, ctx, 4, cycle_cnt)
        results["t4_tx"] = (cycle_cnt, t4_bytes + more)

        # ──────────────────────────────────────────────────────────────
        # Test 5: BCP_START false_lit=13, conflict clause_id=7
        # Expected TX: [0xC1, 0x00, 0x07, 0x00]
        # ──────────────────────────────────────────────────────────────
        for _ in range(4):
            await ctx.tick(); cycle_cnt += 1

        await send_cmd(dut, ctx, CMD_BCP_START, [0x00, 0x0D], cycle_cnt)

        for _ in range(3):
            await ctx.tick(); cycle_cnt += 1
        ctx.set(dut.bcp_done,        1)
        ctx.set(dut.bcp_conflict,    1)
        ctx.set(dut.bcp_conflict_id, 7)
        await ctx.tick();   cycle_cnt += 1
        ctx.set(dut.bcp_done,        0)
        ctx.set(dut.bcp_conflict,    0)

        results["t5_tx"] = (cycle_cnt, await collect_tx_bytes(dut, ctx, 4, cycle_cnt))

    sim = Simulator(dut)
    sim.add_clock(1e-8)
    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__), "host_interface_sim.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()

    # ── assertions ────────────────────────────────────────────────────────
    all_pass = True

    def check(name, got_tuple, expected):
        nonlocal all_pass
        cycle, got = got_tuple
        time_ns = cycle * 10   # because your clock is 10 ns
        ok = got == expected
        if not ok:
            all_pass = False
        print(f"[cycle {cycle} | {time_ns} ns] "
            f"{'PASS' if ok else 'FAIL'}  {name}: "
            f"expected={expected!r}  got={got!r}")
        
    print("\nHostInterface testbench results:")

    # Test 1
    check("T1 assign_wr_en",   results["t1_en"],   1)
    check("T1 assign_wr_addr", results["t1_addr"], 5)
    check("T1 assign_wr_data", results["t1_data"], 2)

    # Test 2
    check("T2 clause_wr_en",   results["t2_en"],   1)
    check("T2 clause_wr_addr", results["t2_addr"], 3)
    check("T2 clause_wr_lit0", results["t2_lit0"], 6)
    check("T2 clause_wr_lit1", results["t2_lit1"], 9)

    # Test 3: done-ok packet
    check("T3 done-ok TX", results["t3_tx"],
          [RSP_DONE_OK, 0x00, 0x00, 0x00])

    # Test 4: one implication then done-ok
    check("T4 impl+done TX", results["t4_tx"],
          [RSP_IMPLICATION, 0x00, 0x06, 0x01, 0x00, 0x03,
           RSP_DONE_OK,     0x00, 0x00, 0x00])

    # Test 5: done-conflict with clause_id=7
    check("T5 done-conflict TX", results["t5_tx"],
          [RSP_DONE_CONF, 0x00, 0x07, 0x00])

    if all_pass:
        print("\nAll tests PASSED.")
    else:
        print("\nSome tests FAILED.")
        raise AssertionError("HostInterface testbench failed")


if __name__ == "__main__":
    test_host_interface()
