"""
Hardware BCP simulation bridge — drives BCPAccelerator directly.

Runs the Python CDCL solver inside an Amaranth testbench coroutine,
delegating BCP to the hardware accelerator via direct memory-port writes
(no JTAG). Memory interface latency is assumed negligible — we measure
only the BCP pipeline cycles.
"""

import sys
import os
from dataclasses import dataclass, field

# Add hardware source to path
_hw_dir = os.path.join(os.path.dirname(__file__), "..", "src", "hardware")
if _hw_dir not in sys.path:
    sys.path.insert(0, _hw_dir)

from amaranth import *
from amaranth.sim import Simulator

from modules.bcp_accelerator import BCPAccelerator

from .dimacs_parser import CNFFormula, lit_to_code, lit_neg, lit_var
from .cdcl_solver import CDCLSolver, SolverStats, UNASSIGNED

# Hardware assignment encoding
HW_UNASSIGNED = 0
HW_FALSE = 1
HW_TRUE = 2


def sw_to_hw_assign(val):
    """Map software assignment (-1/0/1) to hardware encoding (0/1/2)."""
    if val == 1:
        return HW_TRUE
    if val == 0:
        return HW_FALSE
    return HW_UNASSIGNED


# ── Cycle counters ────────────────────────────────────────────────────────

@dataclass
class CycleCounters:
    total_cycles: int = 0        # total sync clock ticks in solve loop
    bcp_cycles: int = 0          # sync ticks inside BCP rounds (start→done)
    bcp_rounds: int = 0          # number of BCP_START pulses issued
    implications: int = 0        # total implications received from HW
    conflicts: int = 0           # conflicts detected by HW
    init_cycles: int = 0         # cycles for memory initialization
    sync_cycles: int = 0         # cycles for assignment sync after backtrack
    per_round_cycles: list = field(default_factory=list)


# ── Hardware BCP Simulator ────────────────────────────────────────────────

class HWBCPSimulator:
    """Run CDCL solver with hardware-accelerated BCP in Amaranth simulation."""

    def __init__(self, formula: CNFFormula, verbose=False):
        self.formula = formula
        self.verbose = verbose
        self.counters = CycleCounters()
        self.solver = CDCLSolver(formula)
        self._result = None
        self._stats = None

    def run(self):
        """Run the full solve in simulation. Returns (is_sat, stats, counters)."""
        dut = BCPAccelerator()
        sim = Simulator(dut)
        sim.add_clock(1e-8)  # 100 MHz sync

        async def testbench(ctx):
            is_sat = await self._run_solve(ctx, dut)
            self._result = is_sat
            self._stats = self.solver.stats

        sim.add_testbench(testbench)
        sim.run()

        return self._result, self._stats, self.counters

    # ── Direct memory write helpers (1 cycle each) ────────────────────────

    @staticmethod
    async def _write_clause(ctx, dut, cid, size, sat_bit, lits):
        """Write a clause to clause memory. 1 sync cycle."""
        ctx.set(dut.clause_wr_addr, cid)
        ctx.set(dut.clause_wr_sat_bit, sat_bit)
        ctx.set(dut.clause_wr_size, size)
        ctx.set(dut.clause_wr_lit0, lits[0] if len(lits) > 0 else 0)
        ctx.set(dut.clause_wr_lit1, lits[1] if len(lits) > 1 else 0)
        ctx.set(dut.clause_wr_lit2, lits[2] if len(lits) > 2 else 0)
        ctx.set(dut.clause_wr_lit3, lits[3] if len(lits) > 3 else 0)
        ctx.set(dut.clause_wr_lit4, lits[4] if len(lits) > 4 else 0)
        ctx.set(dut.clause_wr_en, 1)
        await ctx.tick()
        ctx.set(dut.clause_wr_en, 0)

    @staticmethod
    async def _write_watch_list(ctx, dut, lit_code, clause_ids):
        """Write a watch list (length + entries). len(clause_ids)+1 cycles."""
        ctx.set(dut.wl_wr_lit, lit_code)
        ctx.set(dut.wl_wr_len, len(clause_ids))
        ctx.set(dut.wl_wr_len_en, 1)
        await ctx.tick()
        ctx.set(dut.wl_wr_len_en, 0)
        for idx, cid in enumerate(clause_ids):
            ctx.set(dut.wl_wr_lit, lit_code)
            ctx.set(dut.wl_wr_idx, idx)
            ctx.set(dut.wl_wr_data, cid)
            ctx.set(dut.wl_wr_en, 1)
            await ctx.tick()
            ctx.set(dut.wl_wr_en, 0)

    @staticmethod
    async def _write_assign(ctx, dut, var, hw_val):
        """Write a variable assignment. 1 sync cycle."""
        ctx.set(dut.assign_wr_addr, var)
        ctx.set(dut.assign_wr_data, hw_val)
        ctx.set(dut.assign_wr_en, 1)
        await ctx.tick()
        ctx.set(dut.assign_wr_en, 0)

    @staticmethod
    async def _start_bcp(ctx, dut, false_lit):
        """Pulse BCP start. 1 sync cycle."""
        ctx.set(dut.false_lit, false_lit)
        ctx.set(dut.start, 1)
        await ctx.tick()
        ctx.set(dut.start, 0)

    @staticmethod
    async def _wait_done(ctx, dut, max_cycles=5000):
        """Wait for done signal. Returns cycle count."""
        for i in range(max_cycles):
            if ctx.get(dut.done):
                return i + 1
            await ctx.tick()
        raise RuntimeError(f"BCP timed out after {max_cycles} cycles")

    @staticmethod
    async def _pop_implication(ctx, dut):
        """Pop one implication from FIFO. Returns (var, value, reason)."""
        var = ctx.get(dut.impl_var)
        value = ctx.get(dut.impl_value)
        reason = ctx.get(dut.impl_reason)
        ctx.set(dut.impl_ready, 1)
        await ctx.tick()
        ctx.set(dut.impl_ready, 0)
        return var, value, reason

    # ── Initialization ────────────────────────────────────────────────────

    async def _hw_init(self, ctx, dut):
        """Upload all clauses, watch lists, assignments to hardware memory."""
        solver = self.solver
        cycle_start = self.counters.total_cycles

        # 1. Upload clauses
        for ci, c in enumerate(solver.clauses):
            size = min(c.size, 5)
            await self._write_clause(ctx, dut, ci, size, 0, c.lits[:size])
            self.counters.total_cycles += 1

        # 2. Upload watch lists
        num_lits = 2 * solver.num_vars + 2
        for lit_code in range(num_lits):
            wlist = solver.watches[lit_code]
            if not wlist:
                continue
            await self._write_watch_list(ctx, dut, lit_code, wlist)
            self.counters.total_cycles += 1 + len(wlist)

        # 3. Upload assignments (all unassigned at start)
        for var in range(1, solver.num_vars + 1):
            await self._write_assign(ctx, dut, var,
                                     sw_to_hw_assign(solver.assigns[var]))
            self.counters.total_cycles += 1

        self.counters.init_cycles = self.counters.total_cycles - cycle_start

        if self.verbose:
            print(f"  HW init: {self.counters.init_cycles} cycles, "
                  f"{len(solver.clauses)} clauses, {solver.num_vars} vars")

    # ── Main solve loop ───────────────────────────────────────────────────

    async def _run_solve(self, ctx, dut):
        """Full CDCL solve with hardware BCP."""
        solver = self.solver

        # Upload formula to hardware
        await self._hw_init(ctx, dut)

        # Handle initial unit clauses (in software)
        for i, c in enumerate(solver.clauses):
            if c.size == 0:
                return False
            if c.size == 1:
                if solver._lit_value(c.lits[0]) == 0:
                    return False
                if solver._lit_value(c.lits[0]) == UNASSIGNED:
                    solver._enqueue(c.lits[0], i)
                    var = lit_var(c.lits[0])
                    await self._write_assign(ctx, dut, var,
                                             sw_to_hw_assign(solver.assigns[var]))
                    self.counters.total_cycles += 1

        while True:
            conflict = await self._hw_propagate(ctx, dut)

            if conflict >= 0:
                solver.stats.conflicts += 1
                if solver.num_decisions == 0:
                    return False

                learnt_lits, bt_level = solver._analyze(conflict)
                solver._backtrack(bt_level)

                # Sync unassigned vars to HW
                await self._hw_sync_assigns(ctx, dut)

                if len(learnt_lits) == 1:
                    solver._enqueue(learnt_lits[0], -1)
                    var = lit_var(learnt_lits[0])
                    await self._write_assign(ctx, dut, var,
                                             sw_to_hw_assign(solver.assigns[var]))
                    self.counters.total_cycles += 1
                else:
                    ci = solver._add_learnt_clause(learnt_lits)
                    solver._enqueue(learnt_lits[0], ci)
                    await self._hw_upload_learnt(ctx, dut, ci)
                    var = lit_var(learnt_lits[0])
                    await self._write_assign(ctx, dut, var,
                                             sw_to_hw_assign(solver.assigns[var]))
                    self.counters.total_cycles += 1
            else:
                dec_var = solver._pick_decision_var()
                if dec_var == 0:
                    return True  # SAT

                solver.stats.decisions += 1
                solver.trail_delimiters.append(len(solver.trail))
                solver.num_decisions += 1

                dec_lit = lit_to_code(-dec_var)
                solver._enqueue(dec_lit, -1)

                # Write decision to HW
                await self._write_assign(ctx, dut, dec_var,
                                         sw_to_hw_assign(solver.assigns[dec_var]))
                self.counters.total_cycles += 1

    # ── Hardware BCP propagation ──────────────────────────────────────────

    async def _hw_propagate(self, ctx, dut):
        """Run hardware BCP for each pending trail entry.
        Returns conflict clause index, or -1."""
        solver = self.solver

        while solver.prop_head < len(solver.trail):
            true_lit = solver.trail[solver.prop_head]
            false_lit = true_lit ^ 1
            solver.prop_head += 1
            solver.stats.propagations += 1
            self.counters.bcp_rounds += 1

            # Start BCP
            await self._start_bcp(ctx, dut, false_lit)
            bcp_cycles = await self._wait_done(ctx, dut)
            self.counters.bcp_cycles += bcp_cycles
            self.counters.total_cycles += bcp_cycles + 1  # +1 for start pulse

            # Check conflict
            if ctx.get(dut.conflict):
                conflict_cid = ctx.get(dut.conflict_clause_id)
                self.counters.conflicts += 1
                self.counters.per_round_cycles.append(bcp_cycles)
                # Acknowledge the conflict so FSM returns to IDLE
                ctx.set(dut.conflict_ack, 1)
                await ctx.tick()
                ctx.set(dut.conflict_ack, 0)
                self.counters.total_cycles += 1
                await ctx.tick()  # DONE -> IDLE
                self.counters.total_cycles += 1
                # Apply valid implications from clauses processed before
                # the conflicting clause (they are already in the FIFO).
                while ctx.get(dut.impl_valid):
                    var, value, reason = await self._pop_implication(ctx, dut)
                    self.counters.total_cycles += 1
                    if solver.assigns[var] != UNASSIGNED:
                        continue
                    if value == 1:
                        code = 2 * var
                    else:
                        code = 2 * var + 1
                    solver.assigns[var] = 0 if (code & 1) else 1
                    solver.levels[var] = solver.num_decisions
                    solver.reasons[var] = reason
                    solver.trail.append(code)
                    solver.stats.implications += 1
                    await self._write_assign(ctx, dut, var,
                                             sw_to_hw_assign(solver.assigns[var]))
                    self.counters.total_cycles += 1
                return conflict_cid

            await ctx.tick()  # DONE -> IDLE
            self.counters.total_cycles += 1

            # Pop implications from FIFO, apply to SW and HW.
            # The HW evaluates all clauses with the same assignment snapshot,
            # so it may produce:
            #   - Redundant implications (var already assigned same value): skip
            #   - Contradictory implications (var assigned opposite value):
            #     this is a conflict the HW missed because it didn't see the
            #     earlier implication's effect on assignments
            sw_conflict = -1
            while ctx.get(dut.impl_valid):
                var, value, reason = await self._pop_implication(ctx, dut)
                self.counters.implications += 1
                self.counters.total_cycles += 1

                if solver.assigns[var] != UNASSIGNED:
                    # Check for contradictory implication (missed conflict)
                    expected = 1 if value == 1 else 0
                    if solver.assigns[var] != expected and sw_conflict < 0:
                        sw_conflict = reason
                    continue

                # Enqueue in software trail
                if value == 1:  # HW: 1=TRUE
                    code = 2 * var
                else:
                    code = 2 * var + 1

                solver.assigns[var] = 0 if (code & 1) else 1
                solver.levels[var] = solver.num_decisions
                solver.reasons[var] = reason
                solver.trail.append(code)
                solver.stats.implications += 1

                # Write new assignment to HW
                await self._write_assign(ctx, dut, var,
                                         sw_to_hw_assign(solver.assigns[var]))
                self.counters.total_cycles += 1

            self.counters.per_round_cycles.append(bcp_cycles)

            if sw_conflict >= 0:
                self.counters.conflicts += 1
                return sw_conflict

        return -1  # no conflict

    # ── Assignment sync after backtrack ───────────────────────────────────

    async def _hw_sync_assigns(self, ctx, dut):
        """Write all unassigned vars to HW after backtrack."""
        solver = self.solver
        for var in range(1, solver.num_vars + 1):
            if solver.assigns[var] == UNASSIGNED:
                await self._write_assign(ctx, dut, var, HW_UNASSIGNED)
                self.counters.sync_cycles += 1
                self.counters.total_cycles += 1

    # ── Upload learned clause ─────────────────────────────────────────────

    async def _hw_upload_learnt(self, ctx, dut, ci):
        """Upload a learned clause + watch list entries to HW."""
        solver = self.solver
        c = solver.clauses[ci]
        size = min(c.size, 5)

        await self._write_clause(ctx, dut, ci, size, 0, c.lits[:size])
        self.counters.total_cycles += 1

        # Update watch lists for first two literals
        if size >= 2:
            for w_idx in range(2):
                lit_code = c.lits[w_idx]
                wlist = solver.watches[lit_code]
                wlen = len(wlist)
                await self._write_watch_list(ctx, dut, lit_code, wlist)
                self.counters.total_cycles += 1 + wlen
