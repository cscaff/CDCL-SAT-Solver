"""
Testbench for the Watch List Memory module.

Verifies:
  1. Default reads return 0 (length=0, clause_id=0).
  2. Write a length and clause IDs for one literal, read back after 2 cycles.
  3. Write watch lists for multiple literals with distinct data, read each back.
  4. Overwrite a watch list entry and verify update.
  5. Verify the spec example content (literal encodings and their watch lists).
"""

import sys, os

# Add src/ to the path so we can import the module
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from memory.watch_list_memory import WatchListMemory


def test_watch_list_memory():
    dut = WatchListMemory(num_literals=1024, max_watch_len=100)
    sim = Simulator(dut)
    sim.add_clock(1e-8)  # 100 MHz

    async def testbench(ctx):
        # Helper: issue a read and wait 2 cycles for data
        async def read_watch(lit, idx):
            ctx.set(dut.rd_lit, lit)
            ctx.set(dut.rd_idx, idx)
            ctx.set(dut.rd_en, 1)
            await ctx.tick()  # Cycle 1: BRAM latches address; pipe1 captures rd_en=1
            ctx.set(dut.rd_en, 0)
            await ctx.tick()  # Cycle 2: stage2 captures BRAM output; pipe2 captures pipe1=1
            # Now rd_valid should be asserted and data available
            valid = ctx.get(dut.rd_valid)
            assert valid == 1, f"rd_valid not asserted after 2-cycle read"
            return {
                "len": ctx.get(dut.rd_len),
                "data": ctx.get(dut.rd_data),
            }

        # Helper: write a clause ID entry
        async def write_clause_id(lit, idx, clause_id):
            ctx.set(dut.wr_lit, lit)
            ctx.set(dut.wr_idx, idx)
            ctx.set(dut.wr_data, clause_id)
            ctx.set(dut.wr_en, 1)
            await ctx.tick()
            ctx.set(dut.wr_en, 0)

        # Helper: write a length
        async def write_length(lit, length):
            ctx.set(dut.wr_lit, lit)
            ctx.set(dut.wr_len, length)
            ctx.set(dut.wr_len_en, 1)
            await ctx.tick()
            ctx.set(dut.wr_len_en, 0)

        # ---- Test 1: Default reads return 0 ----
        for lit in [0, 1, 100, 1023]:
            d = await read_watch(lit, 0)
            assert d["len"] == 0, f"Test 1 FAIL: lit {lit} len != 0"
            assert d["data"] == 0, f"Test 1 FAIL: lit {lit} data != 0"
        print("Test 1 PASSED: Default reads return all zeros.")

        # ---- Test 2: Write a length and clause IDs for one literal ----
        # Literal 2 (encoding for variable a): watch list = [0, 2, 4], length = 3
        await write_length(2, 3)
        await write_clause_id(2, 0, 0)
        await write_clause_id(2, 1, 2)
        await write_clause_id(2, 2, 4)

        # Read back length (from idx=0 read)
        d = await read_watch(2, 0)
        assert d["len"] == 3, f"Test 2 FAIL: len expected 3, got {d['len']}"
        assert d["data"] == 0, f"Test 2 FAIL: clause_id[0] expected 0, got {d['data']}"

        d = await read_watch(2, 1)
        assert d["data"] == 2, f"Test 2 FAIL: clause_id[1] expected 2, got {d['data']}"

        d = await read_watch(2, 2)
        assert d["data"] == 4, f"Test 2 FAIL: clause_id[2] expected 4, got {d['data']}"
        print("Test 2 PASSED: Write and read back a single literal's watch list.")

        # ---- Test 3: Write watch lists for multiple literals ----
        # Spec example watch lists:
        #   lit 2 (a):   len=3, clause_ids=[0, 2, 4]    (already written)
        #   lit 3 (¬a):  len=1, clause_ids=[1]
        #   lit 4 (b):   len=1, clause_ids=[0]
        #   lit 5 (¬b):  len=1, clause_ids=[2]
        #   lit 6 (c):   len=2, clause_ids=[0, 3]
        #   lit 8 (d):   len=1, clause_ids=[1]
        #   lit 9 (¬d):  len=1, clause_ids=[2]
        #   lit 10 (e):  len=1, clause_ids=[2]
        #   lit 11 (¬e): len=1, clause_ids=[3]

        watch_lists = {
            3:  (1, [1]),
            4:  (1, [0]),
            5:  (1, [2]),
            6:  (2, [0, 3]),
            8:  (1, [1]),
            9:  (1, [2]),
            10: (1, [2]),
            11: (1, [3]),
        }

        for lit, (length, cids) in watch_lists.items():
            await write_length(lit, length)
            for idx, cid in enumerate(cids):
                await write_clause_id(lit, idx, cid)

        # Read back all watch lists
        for lit, (length, cids) in watch_lists.items():
            d = await read_watch(lit, 0)
            assert d["len"] == length, (
                f"Test 3 FAIL: lit {lit} len expected {length}, got {d['len']}"
            )
            for idx, expected_cid in enumerate(cids):
                d = await read_watch(lit, idx)
                assert d["data"] == expected_cid, (
                    f"Test 3 FAIL: lit {lit} clause_id[{idx}] expected {expected_cid}, got {d['data']}"
                )
        print("Test 3 PASSED: Multiple literals with distinct watch lists.")

        # ---- Test 4: Overwrite a watch list entry and verify ----
        # Change literal 2, index 1 from clause_id=2 to clause_id=99
        await write_clause_id(2, 1, 99)
        d = await read_watch(2, 1)
        assert d["data"] == 99, f"Test 4 FAIL: expected 99, got {d['data']}"

        # Also update the length of literal 2 from 3 to 5
        await write_length(2, 5)
        d = await read_watch(2, 0)
        assert d["len"] == 5, f"Test 4 FAIL: len expected 5, got {d['len']}"
        print("Test 4 PASSED: Overwrite updates correctly.")

        # ---- Test 5: Verify spec example content ----
        # Re-verify the watch lists written in Test 3 are still intact
        # (literal 2 was modified in Test 4, so skip it here)
        spec_examples = [
            (3,  1, [1]),
            (4,  1, [0]),
            (5,  1, [2]),
            (6,  2, [0, 3]),
            (8,  1, [1]),
            (9,  1, [2]),
            (10, 1, [2]),
            (11, 1, [3]),
        ]
        for lit, length, cids in spec_examples:
            d = await read_watch(lit, 0)
            assert d["len"] == length, (
                f"Test 5 FAIL: lit {lit} len expected {length}, got {d['len']}"
            )
            for idx, expected_cid in enumerate(cids):
                d = await read_watch(lit, idx)
                assert d["data"] == expected_cid, (
                    f"Test 5 FAIL: lit {lit} clause_id[{idx}] expected {expected_cid}, got {d['data']}"
                )
        print("Test 5 PASSED: Spec example watch lists verified.")

        print("\nAll tests PASSED.")

    sim.add_testbench(testbench)

    with sim.write_vcd(os.path.join(os.path.dirname(__file__), "..", "..", "logs", "watch_list_memory.vcd")):
        sim.run()


if __name__ == "__main__":
    test_watch_list_memory()
