"""
Testbench for the Clause Database Memory module.

Verifies:
  1. Default reads return 0 (all fields zero).
  2. Write a clause, read it back after 2 cycles, verify all fields.
  3. Write multiple clauses with distinct data, read each back.
  4. Overwrite a clause and verify the update.
  5. Verify the example content from the spec (clauses for (a∨b∨c), (¬a∨d), etc.).
"""

import sys, os

# Add src/ to the path so we can import the module
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from memory.clause_memory import ClauseMemory


def test_clause_memory():
    dut = ClauseMemory(max_clauses=8192)
    sim = Simulator(dut)
    sim.add_clock(1e-8)  # 100 MHz

    async def testbench(ctx):
        # Helper: issue a read and wait 2 cycles for data
        async def read_clause(addr):
            ctx.set(dut.rd_addr, addr)
            ctx.set(dut.rd_en, 1)
            await ctx.tick()  # Cycle 1: BRAM latches address; pipe1 captures rd_en=1
            ctx.set(dut.rd_en, 0)
            await ctx.tick()  # Cycle 2: stage2 captures BRAM output; pipe2 captures pipe1=1
            # Now rd_valid should be asserted and data available
            valid = ctx.get(dut.rd_valid)
            assert valid == 1, f"rd_valid not asserted after 2-cycle read"
            return {
                "sat_bit": ctx.get(dut.rd_data_sat_bit),
                "size": ctx.get(dut.rd_data_size),
                "lit0": ctx.get(dut.rd_data_lit0),
                "lit1": ctx.get(dut.rd_data_lit1),
                "lit2": ctx.get(dut.rd_data_lit2),
                "lit3": ctx.get(dut.rd_data_lit3),
                "lit4": ctx.get(dut.rd_data_lit4),
            }

        # Helper: write a clause in one cycle
        async def write_clause(addr, sat_bit, size, lits):
            ctx.set(dut.wr_addr, addr)
            ctx.set(dut.wr_data_sat_bit, sat_bit)
            ctx.set(dut.wr_data_size, size)
            ctx.set(dut.wr_data_lit0, lits[0])
            ctx.set(dut.wr_data_lit1, lits[1])
            ctx.set(dut.wr_data_lit2, lits[2])
            ctx.set(dut.wr_data_lit3, lits[3])
            ctx.set(dut.wr_data_lit4, lits[4])
            ctx.set(dut.wr_en, 1)
            await ctx.tick()
            ctx.set(dut.wr_en, 0)

        # ---- Test 1: Default reads return 0 ----
        for addr in [0, 1, 100, 8191]:
            d = await read_clause(addr)
            assert d["sat_bit"] == 0, f"Test 1 FAIL: addr {addr} sat_bit != 0"
            assert d["size"] == 0, f"Test 1 FAIL: addr {addr} size != 0"
            assert d["lit0"] == 0, f"Test 1 FAIL: addr {addr} lit0 != 0"
            assert d["lit1"] == 0, f"Test 1 FAIL: addr {addr} lit1 != 0"
            assert d["lit2"] == 0, f"Test 1 FAIL: addr {addr} lit2 != 0"
            assert d["lit3"] == 0, f"Test 1 FAIL: addr {addr} lit3 != 0"
            assert d["lit4"] == 0, f"Test 1 FAIL: addr {addr} lit4 != 0"
        print("Test 1 PASSED: Default reads return all zeros.")

        # ---- Test 2: Write a clause and read it back ----
        # Clause at address 10: sat_bit=0, size=3, lits=[2, 4, 6, 0, 0]
        # Represents (a ∨ b ∨ c) with a=var1→lit2, b=var2→lit4, c=var3→lit6
        await write_clause(10, sat_bit=0, size=3, lits=[2, 4, 6, 0, 0])
        d = await read_clause(10)
        assert d["sat_bit"] == 0, f"Test 2 FAIL: sat_bit"
        assert d["size"] == 3, f"Test 2 FAIL: size expected 3, got {d['size']}"
        assert d["lit0"] == 2, f"Test 2 FAIL: lit0 expected 2, got {d['lit0']}"
        assert d["lit1"] == 4, f"Test 2 FAIL: lit1 expected 4, got {d['lit1']}"
        assert d["lit2"] == 6, f"Test 2 FAIL: lit2 expected 6, got {d['lit2']}"
        assert d["lit3"] == 0, f"Test 2 FAIL: lit3"
        assert d["lit4"] == 0, f"Test 2 FAIL: lit4"
        print("Test 2 PASSED: Write and read back a single clause.")

        # ---- Test 3: Write multiple clauses with distinct data ----
        clauses = {
            0: (0, 3, [2, 4, 6, 0, 0]),         # (a ∨ b ∨ c)
            1: (1, 2, [3, 8, 0, 0, 0]),          # (¬a ∨ d) satisfied
            2: (0, 3, [5, 9, 10, 0, 0]),         # (¬b ∨ ¬d ∨ e)
            3: (0, 2, [6, 11, 0, 0, 0]),         # (c ∨ ¬e)
            500: (0, 5, [2, 3, 4, 5, 6]),        # 5-literal clause
            8191: (1, 1, [100, 0, 0, 0, 0]),     # last address, satisfied, 1 lit
        }
        for addr, (sat, sz, lits) in clauses.items():
            await write_clause(addr, sat_bit=sat, size=sz, lits=lits)

        for addr, (sat, sz, lits) in clauses.items():
            d = await read_clause(addr)
            assert d["sat_bit"] == sat, (
                f"Test 3 FAIL: addr {addr} sat_bit expected {sat}, got {d['sat_bit']}"
            )
            assert d["size"] == sz, (
                f"Test 3 FAIL: addr {addr} size expected {sz}, got {d['size']}"
            )
            for i, exp_lit in enumerate(lits):
                got = d[f"lit{i}"]
                assert got == exp_lit, (
                    f"Test 3 FAIL: addr {addr} lit{i} expected {exp_lit}, got {got}"
                )
        print("Test 3 PASSED: Multiple clauses with distinct data.")

        # ---- Test 4: Overwrite a clause and verify ----
        # Overwrite address 10 (was size=3, lits=[2,4,6,0,0])
        await write_clause(10, sat_bit=1, size=4, lits=[7, 8, 9, 10, 0])
        d = await read_clause(10)
        assert d["sat_bit"] == 1, f"Test 4 FAIL: sat_bit"
        assert d["size"] == 4, f"Test 4 FAIL: size expected 4, got {d['size']}"
        assert d["lit0"] == 7, f"Test 4 FAIL: lit0"
        assert d["lit1"] == 8, f"Test 4 FAIL: lit1"
        assert d["lit2"] == 9, f"Test 4 FAIL: lit2"
        assert d["lit3"] == 10, f"Test 4 FAIL: lit3"
        assert d["lit4"] == 0, f"Test 4 FAIL: lit4"
        print("Test 4 PASSED: Overwrite updates correctly.")

        # ---- Test 5: Verify spec example content ----
        # From the Architecture spec, Memory Module 1 example:
        #   Addr 0: sat=0, size=3, lits=[2,4,6,0,0]      (a ∨ b ∨ c)
        #   Addr 1: sat=1, size=2, lits=[3,8,0,0,0]      (¬a ∨ d) ✓
        #   Addr 2: sat=0, size=3, lits=[5,9,10,0,0]     (¬b ∨ ¬d ∨ e)
        #   Addr 3: sat=0, size=2, lits=[6,11,0,0,0]     (c ∨ ¬e)
        # These were already written in Test 3; verify again explicitly.
        spec_examples = [
            (0, 0, 3, [2, 4, 6, 0, 0]),
            (1, 1, 2, [3, 8, 0, 0, 0]),
            (2, 0, 3, [5, 9, 10, 0, 0]),
            (3, 0, 2, [6, 11, 0, 0, 0]),
        ]
        for addr, sat, sz, lits in spec_examples:
            d = await read_clause(addr)
            assert d["sat_bit"] == sat, (
                f"Test 5 FAIL: addr {addr} sat_bit expected {sat}, got {d['sat_bit']}"
            )
            assert d["size"] == sz, (
                f"Test 5 FAIL: addr {addr} size expected {sz}, got {d['size']}"
            )
            for i, exp_lit in enumerate(lits):
                got = d[f"lit{i}"]
                assert got == exp_lit, (
                    f"Test 5 FAIL: addr {addr} lit{i} expected {exp_lit}, got {got}"
                )
        print("Test 5 PASSED: Spec example clauses verified.")

        print("\nAll tests PASSED.")

    sim.add_testbench(testbench)

    with sim.write_vcd("clause_memory.vcd"):
        sim.run()


if __name__ == "__main__":
    test_clause_memory()
