"""
Testbench for the Clause Prefetcher module.

Uses a real ClauseMemory connected to the Prefetcher so that the
2-cycle read latency is exercised end-to-end.

Verifies:
  1. Single clause fetch: correct data and clause_id after 2-cycle latency.
  2. Back-to-back fetches: pipelined throughput (1 result per cycle once full).
  3. No spurious output: meta_valid stays low when no input is presented.
  4. Clause fields: sat_bit, size, and all 5 literal slots forwarded correctly.
  5. Non-contiguous clause IDs: arbitrary addresses work correctly.
"""

import sys, os

# Add src/ to the path so we can import the module
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from modules.clause_prefetcher import ClausePrefetcher
from memory.clause_memory import ClauseMemory


class PrefetchTestFixture(Elaboratable):
    """Wraps ClausePrefetcher + ClauseMemory with internal wiring."""

    def __init__(self):
        self.pf = ClausePrefetcher()
        self.cmem = ClauseMemory()

    def elaborate(self, platform):
        m = Module()
        m.submodules.pf = self.pf
        m.submodules.cmem = self.cmem

        # Wire Prefetcher → Clause Memory (read port)
        m.d.comb += [
            self.cmem.rd_addr.eq(self.pf.clause_rd_addr),
            self.cmem.rd_en.eq(self.pf.clause_rd_en),
            self.pf.clause_rd_valid.eq(self.cmem.rd_valid),
            self.pf.clause_rd_sat_bit.eq(self.cmem.rd_data_sat_bit),
            self.pf.clause_rd_size.eq(self.cmem.rd_data_size),
            self.pf.clause_rd_lit0.eq(self.cmem.rd_data_lit0),
            self.pf.clause_rd_lit1.eq(self.cmem.rd_data_lit1),
            self.pf.clause_rd_lit2.eq(self.cmem.rd_data_lit2),
            self.pf.clause_rd_lit3.eq(self.cmem.rd_data_lit3),
            self.pf.clause_rd_lit4.eq(self.cmem.rd_data_lit4),
        ]

        return m


def test_clause_prefetcher():
    dut = PrefetchTestFixture()
    pf = dut.pf
    cmem = dut.cmem
    sim = Simulator(dut)
    sim.add_clock(1e-8)  # 100 MHz

    async def testbench(ctx):

        # --- Helpers ---

        async def write_clause(addr, sat_bit, size, lits):
            """Write a clause into the Clause Memory."""
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

        def read_output():
            """Read all output fields from the prefetcher."""
            return {
                "valid": ctx.get(pf.meta_valid),
                "clause_id": ctx.get(pf.clause_id_out),
                "sat_bit": ctx.get(pf.out_sat_bit),
                "size": ctx.get(pf.out_size),
                "lit0": ctx.get(pf.out_lit0),
                "lit1": ctx.get(pf.out_lit1),
                "lit2": ctx.get(pf.out_lit2),
                "lit3": ctx.get(pf.out_lit3),
                "lit4": ctx.get(pf.out_lit4),
            }

        # --- Setup: populate clause memory ---
        # Clause 0: (a ∨ b ∨ c) = lits [2, 4, 6], sat_bit=0, size=3
        await write_clause(0, sat_bit=0, size=3, lits=[2, 4, 6, 0, 0])
        # Clause 1: (¬a ∨ d) = lits [3, 8], sat_bit=1, size=2
        await write_clause(1, sat_bit=1, size=2, lits=[3, 8, 0, 0, 0])
        # Clause 5: (¬b ∨ ¬d ∨ e) = lits [5, 9, 10], sat_bit=0, size=3
        await write_clause(5, sat_bit=0, size=3, lits=[5, 9, 10, 0, 0])
        # Clause 100: full 5-lit clause, sat_bit=0, size=5
        await write_clause(100, sat_bit=0, size=5, lits=[2, 4, 6, 8, 10])

        # Allow a couple of idle cycles
        await ctx.tick()
        await ctx.tick()

        # ---- Test 1: Single clause fetch ----
        ctx.set(pf.clause_id_in, 0)
        ctx.set(pf.clause_id_valid, 1)
        await ctx.tick()                # Edge 1: read issued
        ctx.set(pf.clause_id_valid, 0)
        await ctx.tick()                # Edge 2: pipeline stage 1

        # After edge 2: meta_valid should be 1
        out = read_output()
        assert out["valid"] == 1, f"Test 1 FAIL: meta_valid not asserted"
        assert out["clause_id"] == 0, f"Test 1 FAIL: clause_id"
        assert out["sat_bit"] == 0, f"Test 1 FAIL: sat_bit"
        assert out["size"] == 3, f"Test 1 FAIL: size expected 3, got {out['size']}"
        assert out["lit0"] == 2, f"Test 1 FAIL: lit0"
        assert out["lit1"] == 4, f"Test 1 FAIL: lit1"
        assert out["lit2"] == 6, f"Test 1 FAIL: lit2"
        print("Test 1 PASSED: Single clause fetch with correct data.")

        # meta_valid should drop next cycle (no new input)
        await ctx.tick()
        assert ctx.get(pf.meta_valid) == 0, "Test 1 FAIL: meta_valid should drop"

        # ---- Test 2: Back-to-back pipelined fetches ----
        ctx.set(pf.clause_id_in, 0)
        ctx.set(pf.clause_id_valid, 1)
        await ctx.tick()                # Edge: read clause 0
        ctx.set(pf.clause_id_in, 1)
        await ctx.tick()                # Edge: read clause 1, pipeline filling
        ctx.set(pf.clause_id_valid, 0)

        # After 2nd edge: clause 0 data arrives
        out = read_output()
        assert out["valid"] == 1, "Test 2a FAIL: meta_valid"
        assert out["clause_id"] == 0, f"Test 2a FAIL: clause_id got {out['clause_id']}"
        assert out["size"] == 3, f"Test 2a FAIL: size got {out['size']}"

        await ctx.tick()

        # After 3rd edge: clause 1 data arrives
        out = read_output()
        assert out["valid"] == 1, "Test 2b FAIL: meta_valid"
        assert out["clause_id"] == 1, f"Test 2b FAIL: clause_id got {out['clause_id']}"
        assert out["sat_bit"] == 1, f"Test 2b FAIL: sat_bit"
        assert out["size"] == 2, f"Test 2b FAIL: size got {out['size']}"
        assert out["lit0"] == 3, f"Test 2b FAIL: lit0"
        assert out["lit1"] == 8, f"Test 2b FAIL: lit1"
        print("Test 2 PASSED: Back-to-back pipelined fetches.")

        await ctx.tick()  # let pipeline drain

        # ---- Test 3: No spurious output ----
        # With no input presented, meta_valid should stay 0
        for _ in range(4):
            assert ctx.get(pf.meta_valid) == 0, "Test 3 FAIL: spurious meta_valid"
            await ctx.tick()
        print("Test 3 PASSED: No spurious output when idle.")

        # ---- Test 4: Full 5-literal clause ----
        ctx.set(pf.clause_id_in, 100)
        ctx.set(pf.clause_id_valid, 1)
        await ctx.tick()
        ctx.set(pf.clause_id_valid, 0)
        await ctx.tick()

        out = read_output()
        assert out["valid"] == 1, "Test 4 FAIL: meta_valid"
        assert out["clause_id"] == 100, f"Test 4 FAIL: clause_id"
        assert out["size"] == 5, f"Test 4 FAIL: size"
        assert out["lit0"] == 2, f"Test 4 FAIL: lit0"
        assert out["lit1"] == 4, f"Test 4 FAIL: lit1"
        assert out["lit2"] == 6, f"Test 4 FAIL: lit2"
        assert out["lit3"] == 8, f"Test 4 FAIL: lit3"
        assert out["lit4"] == 10, f"Test 4 FAIL: lit4"
        print("Test 4 PASSED: Full 5-literal clause fields correct.")

        await ctx.tick()

        # ---- Test 5: Non-contiguous clause ID ----
        ctx.set(pf.clause_id_in, 5)
        ctx.set(pf.clause_id_valid, 1)
        await ctx.tick()
        ctx.set(pf.clause_id_valid, 0)
        await ctx.tick()

        out = read_output()
        assert out["valid"] == 1, "Test 5 FAIL: meta_valid"
        assert out["clause_id"] == 5, f"Test 5 FAIL: clause_id"
        assert out["sat_bit"] == 0, f"Test 5 FAIL: sat_bit"
        assert out["size"] == 3, f"Test 5 FAIL: size"
        assert out["lit0"] == 5, f"Test 5 FAIL: lit0"
        assert out["lit1"] == 9, f"Test 5 FAIL: lit1"
        assert out["lit2"] == 10, f"Test 5 FAIL: lit2"
        print("Test 5 PASSED: Non-contiguous clause ID.")

        print("\nAll tests PASSED.")

    sim.add_testbench(testbench)

    with sim.write_vcd(os.path.join(os.path.dirname(__file__), "..", "..", "logs", "clause_prefetcher.vcd")):
        sim.run()


if __name__ == "__main__":
    test_clause_prefetcher()
