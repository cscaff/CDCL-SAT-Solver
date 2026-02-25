"""
Testbench for the Implication FIFO module.

Verifies:
  1. Empty on reset: fifo_empty=1, fifo_full=0, pop_valid=0.
  2. Single push/pop round-trip with correct field unpacking.
  3. Fill to full (16 entries), verify fifo_full, then pop all in FIFO order.
  4. Backpressure: push when full is ignored.
  5. Pop when empty is a no-op.
  6. Simultaneous push+pop: count stays the same.
"""

import sys, os

# Add src/ to the path so we can import the module
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from modules.implication_fifo import ImplicationFIFO, DEFAULT_FIFO_DEPTH


DEPTH = DEFAULT_FIFO_DEPTH  # 16


def test_implication_fifo():
    dut = ImplicationFIFO(fifo_depth=DEPTH)
    sim = Simulator(dut)
    sim.add_clock(1e-8)  # 100 MHz

    async def testbench(ctx):
        # Helper: push one entry (sets signals, ticks, then deasserts)
        async def push(var, value, reason):
            ctx.set(dut.push_valid, 1)
            ctx.set(dut.push_var, var)
            ctx.set(dut.push_value, value)
            ctx.set(dut.push_reason, reason)
            await ctx.tick()
            ctx.set(dut.push_valid, 0)

        # Helper: pop one entry (asserts pop_ready for one cycle)
        async def pop():
            ctx.set(dut.pop_ready, 1)
            await ctx.tick()
            ctx.set(dut.pop_ready, 0)

        # ---- Test 1: Empty on reset ----
        assert ctx.get(dut.fifo_empty) == 1, "Test 1 FAIL: fifo_empty should be 1"
        assert ctx.get(dut.fifo_full) == 0, "Test 1 FAIL: fifo_full should be 0"
        assert ctx.get(dut.pop_valid) == 0, "Test 1 FAIL: pop_valid should be 0"
        print("Test 1 PASSED: Empty on reset.")

        # ---- Test 2: Single push/pop ----
        await push(var=42, value=1, reason=100)

        assert ctx.get(dut.fifo_empty) == 0, "Test 2 FAIL: should not be empty"
        assert ctx.get(dut.pop_valid) == 1, "Test 2 FAIL: pop_valid should be 1"
        assert ctx.get(dut.pop_var) == 42, (
            f"Test 2 FAIL: pop_var expected 42, got {ctx.get(dut.pop_var)}"
        )
        assert ctx.get(dut.pop_value) == 1, (
            f"Test 2 FAIL: pop_value expected 1, got {ctx.get(dut.pop_value)}"
        )
        assert ctx.get(dut.pop_reason) == 100, (
            f"Test 2 FAIL: pop_reason expected 100, got {ctx.get(dut.pop_reason)}"
        )

        # Pop the entry
        await pop()
        assert ctx.get(dut.fifo_empty) == 1, "Test 2 FAIL: should be empty after pop"
        print("Test 2 PASSED: Single push/pop with correct fields.")

        # ---- Test 3: Fill to full, then drain ----
        entries = [(i, i % 2, i * 10) for i in range(DEPTH)]
        for var, value, reason in entries:
            await push(var=var, value=value, reason=reason)

        assert ctx.get(dut.fifo_full) == 1, "Test 3 FAIL: fifo_full should be 1"
        assert ctx.get(dut.fifo_empty) == 0, "Test 3 FAIL: should not be empty"

        # Drain and verify FIFO order
        for idx, (exp_var, exp_val, exp_reason) in enumerate(entries):
            assert ctx.get(dut.pop_valid) == 1, (
                f"Test 3 FAIL: pop_valid should be 1 at entry {idx}"
            )
            got_var = ctx.get(dut.pop_var)
            got_val = ctx.get(dut.pop_value)
            got_reason = ctx.get(dut.pop_reason)
            assert got_var == exp_var, (
                f"Test 3 FAIL: entry {idx} var expected {exp_var}, got {got_var}"
            )
            assert got_val == exp_val, (
                f"Test 3 FAIL: entry {idx} value expected {exp_val}, got {got_val}"
            )
            assert got_reason == exp_reason, (
                f"Test 3 FAIL: entry {idx} reason expected {exp_reason}, got {got_reason}"
            )
            await pop()

        assert ctx.get(dut.fifo_empty) == 1, "Test 3 FAIL: should be empty after drain"
        print("Test 3 PASSED: Fill to full and drain in FIFO order.")

        # ---- Test 4: Backpressure (push when full is ignored) ----
        # Fill again
        for var, value, reason in entries:
            await push(var=var, value=value, reason=reason)
        assert ctx.get(dut.fifo_full) == 1, "Test 4 FAIL: should be full"

        # Attempt one more push — should be ignored
        await push(var=511, value=1, reason=8191)

        # Head should still be the first entry, not the overflow entry
        assert ctx.get(dut.pop_var) == entries[0][0], "Test 4 FAIL: head corrupted"
        assert ctx.get(dut.fifo_full) == 1, "Test 4 FAIL: should still be full"
        print("Test 4 PASSED: Push when full is ignored (backpressure).")

        # Drain for next test
        for _ in range(DEPTH):
            await pop()

        # ---- Test 5: Pop when empty is a no-op ----
        assert ctx.get(dut.fifo_empty) == 1, "Test 5 FAIL: should be empty"
        await pop()
        assert ctx.get(dut.fifo_empty) == 1, "Test 5 FAIL: still empty after pop"
        print("Test 5 PASSED: Pop when empty is a no-op.")

        # ---- Test 6: Simultaneous push+pop ----
        # Put one entry in the FIFO first
        await push(var=10, value=0, reason=50)
        assert ctx.get(dut.fifo_empty) == 0, "Test 6 FAIL: should not be empty"

        # Simultaneous push+pop in a single cycle
        ctx.set(dut.push_valid, 1)
        ctx.set(dut.push_var, 20)
        ctx.set(dut.push_value, 1)
        ctx.set(dut.push_reason, 99)
        ctx.set(dut.pop_ready, 1)
        await ctx.tick()
        ctx.set(dut.push_valid, 0)
        ctx.set(dut.pop_ready, 0)

        # Count should still be 1 (one consumed, one added)
        assert ctx.get(dut.fifo_empty) == 0, "Test 6 FAIL: should not be empty"
        assert ctx.get(dut.fifo_full) == 0, "Test 6 FAIL: should not be full"

        # The new head should be the entry we just pushed (var=20)
        assert ctx.get(dut.pop_var) == 20, (
            f"Test 6 FAIL: expected var 20, got {ctx.get(dut.pop_var)}"
        )
        assert ctx.get(dut.pop_value) == 1, (
            f"Test 6 FAIL: expected value 1, got {ctx.get(dut.pop_value)}"
        )
        assert ctx.get(dut.pop_reason) == 99, (
            f"Test 6 FAIL: expected reason 99, got {ctx.get(dut.pop_reason)}"
        )
        print("Test 6 PASSED: Simultaneous push+pop keeps count unchanged.")

        print("\nAll tests PASSED.")

    sim.add_testbench(testbench)

    with sim.write_vcd(os.path.join(os.path.dirname(__file__), "..", "..", "logs", "implication_fifo.vcd")):
        sim.run()


if __name__ == "__main__":
    test_implication_fifo()
