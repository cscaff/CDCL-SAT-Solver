"""
Testbench for the Variable Assignment Memory module.

Verifies:
  1. All variables initialize to UNASSIGNED (0).
  2. Writing a variable updates its stored value.
  3. Reads reflect the most recent write.
  4. Multiple variables can hold independent values.
  5. Overwriting a variable updates correctly.
"""

import sys, os

# Add src/ to the path so we can import the module
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from memory.assignment_memory import AssignmentMemory, UNASSIGNED, FALSE, TRUE


def test_assignment_memory():
    dut = AssignmentMemory(max_vars=512)
    sim = Simulator(dut)
    sim.add_clock(1e-8)  # 100 MHz

    async def testbench(ctx):
        # ---- Test 1: Default read returns UNASSIGNED ----
        for var_id in [0, 1, 2, 100, 511]:
            ctx.set(dut.rd_addr, var_id)
            # Combinational read — available same delta
            val = ctx.get(dut.rd_data)
            assert val == UNASSIGNED, (
                f"Test 1 FAIL: var {var_id} expected UNASSIGNED(0), got {val}"
            )
        print("Test 1 PASSED: All variables default to UNASSIGNED.")

        # ---- Test 2: Write a single variable and read it back ----
        ctx.set(dut.wr_addr, 0)
        ctx.set(dut.wr_data, TRUE)
        ctx.set(dut.wr_en, 1)
        await ctx.tick()  # Write takes effect on rising edge
        ctx.set(dut.wr_en, 0)

        ctx.set(dut.rd_addr, 0)
        val = ctx.get(dut.rd_data)
        assert val == TRUE, f"Test 2 FAIL: var 0 expected TRUE(2), got {val}"
        print("Test 2 PASSED: Write and read back a single variable.")

        # ---- Test 3: Write multiple variables with distinct values ----
        writes = {
            1: FALSE,     # var 1 = FALSE
            2: TRUE,      # var 2 = TRUE
            3: UNASSIGNED,# var 3 = UNASSIGNED (explicit)
            100: TRUE,    # var 100 = TRUE
            511: FALSE,   # var 511 (last) = FALSE
        }
        for var_id, value in writes.items():
            ctx.set(dut.wr_addr, var_id)
            ctx.set(dut.wr_data, value)
            ctx.set(dut.wr_en, 1)
            await ctx.tick()
        ctx.set(dut.wr_en, 0)

        for var_id, expected in writes.items():
            ctx.set(dut.rd_addr, var_id)
            val = ctx.get(dut.rd_data)
            assert val == expected, (
                f"Test 3 FAIL: var {var_id} expected {expected}, got {val}"
            )
        print("Test 3 PASSED: Multiple independent variable assignments.")

        # ---- Test 4: Overwrite a variable ----
        # var 0 was TRUE; overwrite to FALSE
        ctx.set(dut.wr_addr, 0)
        ctx.set(dut.wr_data, FALSE)
        ctx.set(dut.wr_en, 1)
        await ctx.tick()
        ctx.set(dut.wr_en, 0)

        ctx.set(dut.rd_addr, 0)
        val = ctx.get(dut.rd_data)
        assert val == FALSE, f"Test 4 FAIL: var 0 expected FALSE(1), got {val}"
        print("Test 4 PASSED: Overwrite updates correctly.")

        # ---- Test 5: Unwritten variables remain UNASSIGNED ----
        for var_id in [4, 50, 200, 510]:
            ctx.set(dut.rd_addr, var_id)
            val = ctx.get(dut.rd_data)
            assert val == UNASSIGNED, (
                f"Test 5 FAIL: var {var_id} expected UNASSIGNED(0), got {val}"
            )
        print("Test 5 PASSED: Unwritten variables remain UNASSIGNED.")

        print("\nAll tests PASSED.")

    sim.add_testbench(testbench)

    with sim.write_vcd("assignment_memory.vcd"):
        sim.run()


if __name__ == "__main__":
    test_assignment_memory()
