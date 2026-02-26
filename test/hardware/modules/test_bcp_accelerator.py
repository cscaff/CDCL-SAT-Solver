"""
Testbench for the BCP Accelerator top-level module.

Verifies end-to-end integration of all pipeline stages:
  Watch List Manager -> Clause Prefetcher -> Clause Evaluator -> Implication FIFO
backed by Clause Memory, Watch List Memory, and Assignment Memory.

Tests use single-clause watch lists so that the evaluator (which processes
one clause at a time) always has time to finish before the pipeline drains.

Verifies:
  1. Empty watch list: done asserted quickly, no implications, no conflict.
  2. Single UNIT clause: implication appears in FIFO with correct fields.
  3. Conflict detection: conflict signal latched with correct clause ID.
  4. Satisfied clause (sat_bit): no implication, no conflict, done.
  5. Sequential BCP calls accumulate implications in the FIFO.
  6. Multi-clause watch list (3 clauses) with backpressure -> 3 implications.
"""

import sys, os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from memory.assignment_memory import UNASSIGNED, FALSE, TRUE
from modules.bcp_accelerator import BCPAccelerator


def test_bcp_accelerator():
    dut = BCPAccelerator()
    sim = Simulator(dut)
    sim.add_clock(1e-8)  # 100 MHz

    async def testbench(ctx):

        # ----- Helpers -----

        async def write_clause(addr, sat_bit, size, lits):
            ctx.set(dut.clause_wr_addr, addr)
            ctx.set(dut.clause_wr_sat_bit, sat_bit)
            ctx.set(dut.clause_wr_size, size)
            ctx.set(dut.clause_wr_lit0, lits[0])
            ctx.set(dut.clause_wr_lit1, lits[1])
            ctx.set(dut.clause_wr_lit2, lits[2])
            ctx.set(dut.clause_wr_lit3, lits[3])
            ctx.set(dut.clause_wr_lit4, lits[4])
            ctx.set(dut.clause_wr_en, 1)
            await ctx.tick()
            ctx.set(dut.clause_wr_en, 0)

        async def write_watch_list(lit, clause_ids):
            ctx.set(dut.wl_wr_lit, lit)
            ctx.set(dut.wl_wr_len, len(clause_ids))
            ctx.set(dut.wl_wr_len_en, 1)
            await ctx.tick()
            ctx.set(dut.wl_wr_len_en, 0)
            for idx, cid in enumerate(clause_ids):
                ctx.set(dut.wl_wr_lit, lit)
                ctx.set(dut.wl_wr_idx, idx)
                ctx.set(dut.wl_wr_data, cid)
                ctx.set(dut.wl_wr_en, 1)
                await ctx.tick()
                ctx.set(dut.wl_wr_en, 0)

        async def write_assign(var_id, value):
            ctx.set(dut.assign_wr_addr, var_id)
            ctx.set(dut.assign_wr_data, value)
            ctx.set(dut.assign_wr_en, 1)
            await ctx.tick()
            ctx.set(dut.assign_wr_en, 0)

        async def start_bcp(false_lit):
            ctx.set(dut.false_lit, false_lit)
            ctx.set(dut.start, 1)
            await ctx.tick()
            ctx.set(dut.start, 0)

        async def wait_done(max_cycles=200):
            for _ in range(max_cycles):
                if ctx.get(dut.done):
                    return
                await ctx.tick()
            raise AssertionError("Timed out waiting for done")

        async def ack_conflict():
            """Acknowledge a conflict so the FSM can return to IDLE."""
            ctx.set(dut.conflict_ack, 1)
            await ctx.tick()
            ctx.set(dut.conflict_ack, 0)

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
        # Literal encoding: a=0, ~a=1, b=2, ~b=3, c=4, ~c=5
        #
        # Clause 0: (~a v b)   lits=[1, 2]  size=2  sat_bit=0
        # Clause 1: (~a v ~b)  lits=[1, 3]  size=2  sat_bit=0
        # Clause 2: (~a v c)   lits=[1, 4]  size=2  sat_bit=0
        # Clause 3: (a v b)    lits=[0, 2]  size=2  sat_bit=1  (satisfied)
        #
        await write_clause(0, sat_bit=0, size=2, lits=[1, 2, 0, 0, 0])
        await write_clause(1, sat_bit=0, size=2, lits=[1, 3, 0, 0, 0])
        await write_clause(2, sat_bit=0, size=2, lits=[1, 4, 0, 0, 0])
        await write_clause(3, sat_bit=1, size=2, lits=[0, 2, 0, 0, 0])

        # Watch lists (one clause per literal for Phase 1 testing)
        # lit 1 (~a) watches clause 0  -- used in tests 2, 3, 5
        await write_watch_list(1, [0])
        # lit 3 (~b) watches clause 1  -- used in test 3 (conflict)
        await write_watch_list(3, [1])
        # lit 5 (~c) watches clause 2  -- used in test 5
        await write_watch_list(5, [2])
        # lit 7: empty watch list       -- used in test 1
        await write_watch_list(7, [])
        # lit 0 (a) watches clause 3   -- used in test 4 (satisfied)
        await write_watch_list(0, [3])

        # ---- Test 1: Empty watch list ----
        await start_bcp(false_lit=7)
        await wait_done()
        assert ctx.get(dut.conflict) == 0, "Test 1 FAIL: unexpected conflict"
        assert ctx.get(dut.impl_valid) == 0, "Test 1 FAIL: unexpected implication"
        print("Test 1 PASSED: Empty watch list -> done, no output.")
        await ctx.tick()  # DONE -> IDLE

        # ---- Test 2: Single UNIT implication ----
        # a=TRUE -> ~a (lit 1) becomes false
        # Clause 0: (~a v b), ~a=FALSE, b=UNASSIGNED -> UNIT -> imply b=TRUE
        await write_assign(0, TRUE)   # a = TRUE
        await start_bcp(false_lit=1)
        await wait_done()
        assert ctx.get(dut.conflict) == 0, "Test 2 FAIL: unexpected conflict"
        # Wait a couple cycles for implication to propagate through buffer
        for _ in range(5):
            if ctx.get(dut.impl_valid):
                break
            await ctx.tick()
        assert ctx.get(dut.impl_valid) == 1, "Test 2 FAIL: no implication"
        imp = await pop_implication()
        assert imp["var"] == 1, f"Test 2 FAIL: var expected 1 (b), got {imp['var']}"
        assert imp["value"] == 1, f"Test 2 FAIL: value expected 1 (TRUE), got {imp['value']}"
        assert imp["reason"] == 0, f"Test 2 FAIL: reason expected 0, got {imp['reason']}"
        print("Test 2 PASSED: UNIT clause -> implication b=TRUE, reason=clause 0.")
        await ctx.tick()

        # ---- Test 3: Conflict detection ----
        # a=TRUE, b=TRUE -> ~b (lit 3) becomes false
        # Clause 1: (~a v ~b), ~a=FALSE, ~b=FALSE -> CONFLICT
        await write_assign(1, TRUE)   # b = TRUE
        await start_bcp(false_lit=3)
        await wait_done()
        assert ctx.get(dut.conflict) == 1, "Test 3 FAIL: conflict not detected"
        assert ctx.get(dut.conflict_clause_id) == 1, (
            f"Test 3 FAIL: conflict_clause_id expected 1, "
            f"got {ctx.get(dut.conflict_clause_id)}")
        print("Test 3 PASSED: CONFLICT detected, clause_id=1.")
        # Acknowledge conflict so FSM returns to IDLE
        await ack_conflict()
        await ctx.tick()

        # ---- Test 4: Satisfied clause (sat_bit=1) ----
        # Clause 3 has sat_bit=1 -> evaluator returns SATISFIED immediately
        # No implication, no conflict
        await write_assign(1, UNASSIGNED)  # reset b
        await start_bcp(false_lit=0)
        await wait_done()
        assert ctx.get(dut.conflict) == 0, "Test 4 FAIL: unexpected conflict"
        # FIFO should still be empty (test 2's implication was already popped)
        assert ctx.get(dut.impl_valid) == 0, "Test 4 FAIL: unexpected implication"
        print("Test 4 PASSED: Satisfied clause -> no output, done.")
        await ctx.tick()

        # ---- Test 5: Two sequential BCP calls -> two implications ----
        # Call A: false_lit=1 -> clause 0 (~a v b), a=TRUE, b=UNASSIGNED -> UNIT b=TRUE
        await write_assign(1, UNASSIGNED)  # b unassigned
        await write_assign(2, UNASSIGNED)  # c unassigned
        await start_bcp(false_lit=1)
        await wait_done()
        await ctx.tick()

        # Call B: false_lit=5 -> clause 2 (~a v c), a=TRUE, c=UNASSIGNED -> UNIT c=TRUE
        await start_bcp(false_lit=5)
        await wait_done()
        await ctx.tick()

        # Both implications should be in the FIFO
        # Wait a couple cycles for propagation
        for _ in range(5):
            if ctx.get(dut.impl_valid):
                break
            await ctx.tick()
        assert ctx.get(dut.impl_valid) == 1, "Test 5 FAIL: FIFO empty"
        imp1 = await pop_implication()
        assert imp1["var"] == 1 and imp1["value"] == 1, (
            f"Test 5a FAIL: expected b=TRUE, got var={imp1['var']} val={imp1['value']}")
        # Wait for second implication
        for _ in range(5):
            if ctx.get(dut.impl_valid):
                break
            await ctx.tick()
        assert ctx.get(dut.impl_valid) == 1, "Test 5 FAIL: second impl missing"
        imp2 = await pop_implication()
        assert imp2["var"] == 2 and imp2["value"] == 1, (
            f"Test 5b FAIL: expected c=TRUE, got var={imp2['var']} val={imp2['value']}")
        assert ctx.get(dut.impl_valid) == 0, "Test 5 FAIL: FIFO should be empty"
        print("Test 5 PASSED: Two sequential BCP calls -> two implications.")

        # ---- Test 6: Multi-clause watch list (backpressure) ----
        # Three clauses watching the same literal, each producing UNIT.
        # This exercises the elastic pipeline -- the evaluator can only handle
        # one clause at a time, so WLM/prefetcher must stall.
        #
        # Variables: d=3, e=4, f=5, g=6
        # Literal encoding: d=6, ~d=7, e=8, ~e=9, f=10, ~f=11, g=12, ~g=13
        #
        # Clause 10: (d v e)  lits=[6, 8]   size=2
        # Clause 11: (d v f)  lits=[6, 10]  size=2
        # Clause 12: (d v g)  lits=[6, 12]  size=2
        #
        # Watch list for lit 6 (d): [10, 11, 12]  -- 3 entries!
        # Assignment: d=FALSE (so lit 6 = d becomes false)
        # e, f, g all UNASSIGNED -> each clause is UNIT

        await write_clause(10, sat_bit=0, size=2, lits=[6, 8, 0, 0, 0])
        await write_clause(11, sat_bit=0, size=2, lits=[6, 10, 0, 0, 0])
        await write_clause(12, sat_bit=0, size=2, lits=[6, 12, 0, 0, 0])

        await write_watch_list(6, [10, 11, 12])

        await write_assign(3, FALSE)   # d = FALSE -> lit 6 (d) becomes false
        await write_assign(4, UNASSIGNED)  # e
        await write_assign(5, UNASSIGNED)  # f
        await write_assign(6, UNASSIGNED)  # g

        # Drain any leftover implications from previous tests
        while ctx.get(dut.impl_valid):
            await pop_implication()

        await start_bcp(false_lit=6)
        await wait_done(max_cycles=300)

        assert ctx.get(dut.conflict) == 0, "Test 6 FAIL: unexpected conflict"

        # All 3 implications should be in the FIFO
        implications = []
        # Wait for implications to propagate
        for _ in range(10):
            if ctx.get(dut.impl_valid):
                break
            await ctx.tick()
        for i in range(3):
            # Wait for next implication if needed
            for _ in range(10):
                if ctx.get(dut.impl_valid):
                    break
                await ctx.tick()
            assert ctx.get(dut.impl_valid) == 1, (
                f"Test 6 FAIL: expected 3 implications, only got {i}")
            imp = await pop_implication()
            implications.append(imp)

        # Check we got implications for e, f, g (order may vary)
        implied_vars = sorted([imp["var"] for imp in implications])
        assert implied_vars == [4, 5, 6], (
            f"Test 6 FAIL: expected vars [4,5,6], got {implied_vars}")
        for imp in implications:
            assert imp["value"] == 1, (
                f"Test 6 FAIL: expected value=TRUE for var {imp['var']}")

        assert ctx.get(dut.impl_valid) == 0, "Test 6 FAIL: FIFO should be empty"
        print("Test 6 PASSED: Multi-clause watch list (3 clauses) -> 3 implications.")

        print("\nAll tests PASSED.")

    sim.add_testbench(testbench)

    with sim.write_vcd(os.path.join(os.path.dirname(__file__), "..", "..", "logs", "bcp_accelerator.vcd")):
        sim.run()


if __name__ == "__main__":
    test_bcp_accelerator()
