"""
Testbench for the BCP Accelerator top-level module.

Verifies end-to-end integration of all pipeline stages:
  Watch List Manager → Clause Prefetcher → Clause Evaluator → Implication FIFO
backed by Clause Memory, Watch List Memory, and Assignment Memory.

Tests use single-clause watch lists so that the evaluator (which processes
one clause at a time) always has time to finish before the pipeline drains.

Verifies:
  1. Empty watch list: done asserted quickly, no implications, no conflict.
  2. Single UNIT clause: implication appears in FIFO with correct fields.
  3. Conflict detection: conflict signal latched with correct clause ID.
  4. Satisfied clause (sat_bit): no implication, no conflict, done.
  5. Sequential BCP calls accumulate implications in the FIFO.
"""

import sys, os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from memory.assignment_memory import UNASSIGNED, FALSE, TRUE
from bcp_accelerator import BCPAccelerator


def test_bcp_accelerator():
    dut = BCPAccelerator()
    cmem = dut.clause_mem
    wmem = dut.watch_mem
    amem = dut.assign_mem
    sim = Simulator(dut)
    sim.add_clock(1e-8)  # 100 MHz

    async def testbench(ctx):

        # ----- Helpers -----

        async def write_clause(addr, sat_bit, size, lits):
            ctx.set(cmem.wr_addr, addr)
            ctx.set(cmem.wr_data_sat_bit, sat_bit)
            ctx.set(cmem.wr_data_size, size)
            ctx.set(cmem.wr_data_lit0, lits[0])
            ctx.set(cmem.wr_data_lit1, lits[1])
            ctx.set(cmem.wr_data_lit2, lits[2])
            ctx.set(cmem.wr_data_lit3, lits[3])
            ctx.set(cmem.wr_data_lit4, lits[4])
            ctx.set(cmem.wr_en, 1)
            await ctx.tick()
            ctx.set(cmem.wr_en, 0)

        async def write_watch_list(lit, clause_ids):
            ctx.set(wmem.wr_lit, lit)
            ctx.set(wmem.wr_len, len(clause_ids))
            ctx.set(wmem.wr_len_en, 1)
            await ctx.tick()
            ctx.set(wmem.wr_len_en, 0)
            for idx, cid in enumerate(clause_ids):
                ctx.set(wmem.wr_lit, lit)
                ctx.set(wmem.wr_idx, idx)
                ctx.set(wmem.wr_data, cid)
                ctx.set(wmem.wr_en, 1)
                await ctx.tick()
                ctx.set(wmem.wr_en, 0)

        async def write_assign(var_id, value):
            ctx.set(amem.wr_addr, var_id)
            ctx.set(amem.wr_data, value)
            ctx.set(amem.wr_en, 1)
            await ctx.tick()
            ctx.set(amem.wr_en, 0)

        async def start_bcp(false_lit):
            ctx.set(dut.false_lit, false_lit)
            ctx.set(dut.start, 1)
            await ctx.tick()
            ctx.set(dut.start, 0)

        async def wait_done(max_cycles=60):
            for _ in range(max_cycles):
                if ctx.get(dut.done):
                    return
                await ctx.tick()
            raise AssertionError("Timed out waiting for done")

        async def pop_implication():
            """Pop one entry from the implication FIFO."""
            assert ctx.get(dut.impl_valid) == 1, "No implication to pop"
            result = {
                "var": ctx.get(dut.impl_var),
                "value": ctx.get(dut.impl_value),
                "reason": ctx.get(dut.impl_reason),
            }
            ctx.set(dut.impl_ready, 1)
            await ctx.tick()
            ctx.set(dut.impl_ready, 0)
            return result

        # ============================================================
        # Setup: populate memories
        # ============================================================
        #
        # Variables: a=0, b=1, c=2
        # Literal encoding: a=0, ¬a=1, b=2, ¬b=3, c=4, ¬c=5
        #
        # Clause 0: (¬a ∨ b)   lits=[1, 2]  size=2  sat_bit=0
        # Clause 1: (¬a ∨ ¬b)  lits=[1, 3]  size=2  sat_bit=0
        # Clause 2: (¬a ∨ c)   lits=[1, 4]  size=2  sat_bit=0
        # Clause 3: (a ∨ b)    lits=[0, 2]  size=2  sat_bit=1  (satisfied)
        #
        await write_clause(0, sat_bit=0, size=2, lits=[1, 2, 0, 0, 0])
        await write_clause(1, sat_bit=0, size=2, lits=[1, 3, 0, 0, 0])
        await write_clause(2, sat_bit=0, size=2, lits=[1, 4, 0, 0, 0])
        await write_clause(3, sat_bit=1, size=2, lits=[0, 2, 0, 0, 0])

        # Watch lists (one clause per literal for Phase 1 testing)
        # lit 1 (¬a) watches clause 0  — used in tests 2, 3, 5
        await write_watch_list(1, [0])
        # lit 3 (¬b) watches clause 1  — used in test 3 (conflict)
        await write_watch_list(3, [1])
        # lit 5 (¬c) watches clause 2  — used in test 5
        await write_watch_list(5, [2])
        # lit 7: empty watch list       — used in test 1
        await write_watch_list(7, [])
        # lit 0 (a) watches clause 3   — used in test 4 (satisfied)
        await write_watch_list(0, [3])

        # ---- Test 1: Empty watch list ----
        await start_bcp(false_lit=7)
        await wait_done()
        assert ctx.get(dut.conflict) == 0, "Test 1 FAIL: unexpected conflict"
        assert ctx.get(dut.impl_valid) == 0, "Test 1 FAIL: unexpected implication"
        print("Test 1 PASSED: Empty watch list → done, no output.")
        await ctx.tick()  # DONE → IDLE

        # ---- Test 2: Single UNIT implication ----
        # a=TRUE → ¬a (lit 1) becomes false
        # Clause 0: (¬a ∨ b), ¬a=FALSE, b=UNASSIGNED → UNIT → imply b=TRUE
        await write_assign(0, TRUE)   # a = TRUE
        await start_bcp(false_lit=1)
        await wait_done()
        assert ctx.get(dut.conflict) == 0, "Test 2 FAIL: unexpected conflict"
        assert ctx.get(dut.impl_valid) == 1, "Test 2 FAIL: no implication"
        imp = await pop_implication()
        assert imp["var"] == 1, f"Test 2 FAIL: var expected 1 (b), got {imp['var']}"
        assert imp["value"] == 1, f"Test 2 FAIL: value expected 1 (TRUE), got {imp['value']}"
        assert imp["reason"] == 0, f"Test 2 FAIL: reason expected 0, got {imp['reason']}"
        print("Test 2 PASSED: UNIT clause → implication b=TRUE, reason=clause 0.")
        await ctx.tick()

        # ---- Test 3: Conflict detection ----
        # a=TRUE, b=TRUE → ¬b (lit 3) becomes false
        # Clause 1: (¬a ∨ ¬b), ¬a=FALSE, ¬b=FALSE → CONFLICT
        await write_assign(1, TRUE)   # b = TRUE
        await start_bcp(false_lit=3)
        await wait_done()
        assert ctx.get(dut.conflict) == 1, "Test 3 FAIL: conflict not detected"
        assert ctx.get(dut.conflict_clause_id) == 1, (
            f"Test 3 FAIL: conflict_clause_id expected 1, "
            f"got {ctx.get(dut.conflict_clause_id)}")
        print("Test 3 PASSED: CONFLICT detected, clause_id=1.")
        await ctx.tick()

        # ---- Test 4: Satisfied clause (sat_bit=1) ----
        # Clause 3 has sat_bit=1 → evaluator returns SATISFIED immediately
        # No implication, no conflict
        await write_assign(1, UNASSIGNED)  # reset b
        await start_bcp(false_lit=0)
        await wait_done()
        assert ctx.get(dut.conflict) == 0, "Test 4 FAIL: unexpected conflict"
        # FIFO should still be empty (test 2's implication was already popped)
        assert ctx.get(dut.impl_valid) == 0, "Test 4 FAIL: unexpected implication"
        print("Test 4 PASSED: Satisfied clause → no output, done.")
        await ctx.tick()

        # ---- Test 5: Two sequential BCP calls → two implications ----
        # Call A: false_lit=1 → clause 0 (¬a ∨ b), a=TRUE, b=UNASSIGNED → UNIT b=TRUE
        await write_assign(1, UNASSIGNED)  # b unassigned
        await write_assign(2, UNASSIGNED)  # c unassigned
        await start_bcp(false_lit=1)
        await wait_done()
        await ctx.tick()

        # Call B: false_lit=5 → clause 2 (¬a ∨ c), a=TRUE, c=UNASSIGNED → UNIT c=TRUE
        await start_bcp(false_lit=5)
        await wait_done()
        await ctx.tick()

        # Both implications should be in the FIFO
        assert ctx.get(dut.impl_valid) == 1, "Test 5 FAIL: FIFO empty"
        imp1 = await pop_implication()
        assert imp1["var"] == 1 and imp1["value"] == 1, (
            f"Test 5a FAIL: expected b=TRUE, got var={imp1['var']} val={imp1['value']}")
        assert ctx.get(dut.impl_valid) == 1, "Test 5 FAIL: second impl missing"
        imp2 = await pop_implication()
        assert imp2["var"] == 2 and imp2["value"] == 1, (
            f"Test 5b FAIL: expected c=TRUE, got var={imp2['var']} val={imp2['value']}")
        assert ctx.get(dut.impl_valid) == 0, "Test 5 FAIL: FIFO should be empty"
        print("Test 5 PASSED: Two sequential BCP calls → two implications.")

        print("\nAll tests PASSED.")

    sim.add_testbench(testbench)

    with sim.write_vcd("bcp_accelerator.vcd"):
        sim.run()


if __name__ == "__main__":
    test_bcp_accelerator()
