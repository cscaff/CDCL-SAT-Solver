"""
End-to-end BCP testbench: hardware simulation vs. software golden model.

Runs identical inputs through both a Python reference model of
propagate() and the Amaranth BCP Accelerator.  Each scenario sets up a
CNF formula, initial assignments, and a starting false_lit, then
compares the full implication chain and conflict outcome.

All watch lists contain a single clause so that the Phase-1 evaluator
(one clause at a time) processes every clause without drops.

Literal encoding (same in SW and HW):
    variable v  ->  positive literal = 2v,  negative literal = 2v + 1

HW assignment encoding:  UNASSIGNED=0, FALSE=1, TRUE=2

Scenarios:
  A. Implication chain: a=T → b=T → c=T → d=T  (no conflict)
  B. Chain into conflict: e=T → f=T, then ¬f+¬g → CONFLICT
  C. Empty watch list: immediate done, no output
  D. 3-literal clause with two false: UNIT implication
"""

import sys, os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from memory.assignment_memory import UNASSIGNED as HW_UNASSIGNED
from modules.bcp_accelerator import BCPAccelerator


# ── HW assignment encoding constants ─────────────────────────────────
HW_FALSE = 1
HW_TRUE  = 2


# =====================================================================
#  Python reference model  (mirrors the HW evaluation, one clause/call)
# =====================================================================

def sw_eval_clause(clause, assignments):
    """
    Evaluate a single clause against *assignments* (HW encoding).
    Returns one of:
        ('SATISFIED', None)
        ('UNIT', (var, value, clause_id))
        ('CONFLICT', clause_id)
        ('UNRESOLVED', None)
    """
    if clause["sat_bit"]:
        return ("SATISFIED", None)

    satisfied = False
    unassigned_count = 0
    last_unassigned_lit = None

    for lit in clause["lits"][: clause["size"]]:
        var  = lit >> 1
        sign = lit & 1
        asgn = assignments.get(var, HW_UNASSIGNED)

        if asgn == HW_UNASSIGNED:
            unassigned_count += 1
            last_unassigned_lit = lit
        elif asgn == HW_FALSE:
            if sign == 1:           # ¬v, v=FALSE → ¬v=TRUE
                satisfied = True
        elif asgn == HW_TRUE:
            if sign == 0:           # v, v=TRUE → v=TRUE
                satisfied = True

    if satisfied:
        return ("SATISFIED", None)
    if unassigned_count == 0:
        return ("CONFLICT", clause["id"])
    if unassigned_count == 1:
        implied_var = last_unassigned_lit >> 1
        implied_val = 1 - (last_unassigned_lit & 1)   # ~sign: 0→FALSE, 1→TRUE
        return ("UNIT", (implied_var, implied_val, clause["id"]))
    return ("UNRESOLVED", None)


def sw_bcp_loop(clauses, watch_lists, assignments, initial_false_lit):
    """
    Full BCP propagation loop using the same single-clause-per-call
    model as the Phase-1 hardware.

    Returns (implications, conflict_clause_id_or_None).
    *assignments* is MODIFIED in place (HW encoding).
    """
    trail = [initial_false_lit]
    implications = []  # list of dicts {var, value, reason}

    while trail:
        false_lit = trail.pop(0)
        for cid in watch_lists.get(false_lit, []):
            clause = clauses[cid]
            tag, payload = sw_eval_clause(clause, assignments)

            if tag == "CONFLICT":
                return implications, payload

            if tag == "UNIT":
                imp_var, imp_val, reason = payload
                implications.append(
                    {"var": imp_var, "value": imp_val, "reason": reason}
                )
                assignments[imp_var] = imp_val + 1     # 0→FALSE(1), 1→TRUE(2)
                new_false_lit = 2 * imp_var + imp_val
                trail.append(new_false_lit)

    return implications, None


# =====================================================================
#  Test scenarios  (pure data — no Amaranth dependency)
# =====================================================================

def build_scenarios():
    """
    Returns (clauses_dict, watch_lists, list_of_scenario_dicts).
    Clauses are keyed by ID; watch_lists maps literal → [clause_id, …].
    """

    clauses = {
        # Scenario A  (vars 1–4, lits 2–9)
        # C0: (¬a ∨ b) → [3, 4]
        0: {"id": 0, "sat_bit": 0, "size": 2, "lits": [3, 4, 0, 0, 0]},
        # C1: (¬b ∨ c) → [5, 6]
        1: {"id": 1, "sat_bit": 0, "size": 2, "lits": [5, 6, 0, 0, 0]},
        # C2: (¬c ∨ d) → [7, 8]
        2: {"id": 2, "sat_bit": 0, "size": 2, "lits": [7, 8, 0, 0, 0]},

        # Scenario B  (vars 5–7, lits 10–15)
        # C3: (¬e ∨ f) → [11, 12]
        3: {"id": 3, "sat_bit": 0, "size": 2, "lits": [11, 12, 0, 0, 0]},
        # C4: (¬f ∨ ¬g) → [13, 15]
        4: {"id": 4, "sat_bit": 0, "size": 2, "lits": [13, 15, 0, 0, 0]},

        # Scenario D  (vars 8–10, lits 16–21)
        # C5: (¬h ∨ ¬i ∨ j) → [17, 19, 20]
        5: {"id": 5, "sat_bit": 0, "size": 3, "lits": [17, 19, 20, 0, 0]},
    }

    watch_lists = {
        3:  [0],   # ¬a  → C0
        5:  [1],   # ¬b  → C1
        7:  [2],   # ¬c  → C2
        11: [3],   # ¬e  → C3
        13: [4],   # ¬f  → C4
        17: [5],   # ¬h  → C5
    }

    scenarios = [
        {
            "name": "A: implication chain a→b→c→d",
            "initial_assigns": {1: HW_TRUE},               # a = TRUE
            "initial_false_lit": 3,                         # ¬a
            "vars_used": [1, 2, 3, 4],
        },
        {
            "name": "B: chain into conflict e→f, ¬f∧¬g conflict",
            "initial_assigns": {5: HW_TRUE, 7: HW_TRUE},   # e=T, g=T
            "initial_false_lit": 11,                        # ¬e
            "vars_used": [5, 6, 7],
        },
        {
            "name": "C: empty watch list",
            "initial_assigns": {},
            "initial_false_lit": 100,                       # nothing watches lit 100
            "vars_used": [],
        },
        {
            "name": "D: 3-literal UNIT clause",
            "initial_assigns": {8: HW_TRUE, 9: HW_TRUE},   # h=T, i=T
            "initial_false_lit": 17,                        # ¬h
            "vars_used": [8, 9, 10],
        },
    ]

    return clauses, watch_lists, scenarios


# =====================================================================
#  Hardware test
# =====================================================================

def test_bcp_end_to_end():
    dut = BCPAccelerator()
    cmem  = dut.clause_mem
    wmem  = dut.watch_mem
    amem  = dut.assign_mem
    sim   = Simulator(dut)
    sim.add_clock(1e-8)

    clauses, watch_lists, scenarios = build_scenarios()

    async def testbench(ctx):

        # ── HW memory helpers ────────────────────────────────────────

        async def write_clause(cid, c):
            ctx.set(dut.clause_wr_addr,    cid)
            ctx.set(dut.clause_wr_sat_bit, c["sat_bit"])
            ctx.set(dut.clause_wr_size,    c["size"])
            ctx.set(dut.clause_wr_lit0,    c["lits"][0])
            ctx.set(dut.clause_wr_lit1,    c["lits"][1])
            ctx.set(dut.clause_wr_lit2,    c["lits"][2])
            ctx.set(dut.clause_wr_lit3,    c["lits"][3])
            ctx.set(dut.clause_wr_lit4,    c["lits"][4])
            ctx.set(dut.clause_wr_en,      1)
            await ctx.tick()
            ctx.set(dut.clause_wr_en, 0)

        async def write_watch_list(lit, cids):
            ctx.set(dut.wl_wr_lit,    lit)
            ctx.set(dut.wl_wr_len,    len(cids))
            ctx.set(dut.wl_wr_len_en, 1)
            await ctx.tick()
            ctx.set(dut.wl_wr_len_en, 0)
            for idx, cid in enumerate(cids):
                ctx.set(dut.wl_wr_lit, lit)
                ctx.set(dut.wl_wr_idx, idx)
                ctx.set(dut.wl_wr_data, cid)
                ctx.set(dut.wl_wr_en,  1)
                await ctx.tick()
                ctx.set(dut.wl_wr_en, 0)

        async def write_assign(var, value):
            ctx.set(dut.assign_wr_addr, var)
            ctx.set(dut.assign_wr_data, value)
            ctx.set(dut.assign_wr_en,   1)
            await ctx.tick()
            ctx.set(dut.assign_wr_en, 0)

        async def start_bcp(false_lit):
            ctx.set(dut.false_lit, false_lit)
            ctx.set(dut.start, 1)
            await ctx.tick()
            ctx.set(dut.start, 0)

        async def wait_done(max_cycles=80):
            for _ in range(max_cycles):
                if ctx.get(dut.done):
                    return
                await ctx.tick()
            raise AssertionError("Timed out waiting for done")

        async def pop_implication():
            result = {
                "var":    ctx.get(dut.impl_var),
                "value":  ctx.get(dut.impl_value),
                "reason": ctx.get(dut.impl_reason),
            }
            ctx.set(dut.impl_ready, 1)
            await ctx.tick()
            ctx.set(dut.impl_ready, 0)
            return result

        # ── HW BCP propagation loop ─────────────────────────────────

        async def run_hw_bcp_loop(initial_false_lit):
            trail = [initial_false_lit]
            hw_impls = []
            hw_conflict = None

            while trail:
                false_lit = trail.pop(0)
                await start_bcp(false_lit)
                await wait_done()

                if ctx.get(dut.conflict):
                    hw_conflict = ctx.get(dut.conflict_clause_id)
                    await ctx.tick()          # DONE → IDLE
                    # Drain any leftover FIFO entries
                    while ctx.get(dut.impl_valid):
                        await pop_implication()
                    break

                await ctx.tick()              # DONE → IDLE

                # Pop implications from FIFO, apply, extend trail
                while ctx.get(dut.impl_valid):
                    imp = await pop_implication()
                    hw_impls.append(imp)
                    hw_assign = imp["value"] + 1       # 0→1(FALSE), 1→2(TRUE)
                    await write_assign(imp["var"], hw_assign)
                    new_false_lit = 2 * imp["var"] + imp["value"]
                    trail.append(new_false_lit)

            return hw_impls, hw_conflict

        # ── One-time HW memory initialisation ────────────────────────

        for cid, c in clauses.items():
            await write_clause(cid, c)

        for lit, cids in watch_lists.items():
            await write_watch_list(lit, cids)

        # ── Run each scenario ────────────────────────────────────────

        all_vars_used = set()
        for sc in scenarios:
            all_vars_used.update(sc["vars_used"])

        for sc in scenarios:
            name = sc["name"]

            # Reset assignments for all variables
            for v in all_vars_used:
                await write_assign(v, HW_UNASSIGNED)

            # Write initial assignments into HW
            for var, val in sc["initial_assigns"].items():
                await write_assign(var, val)

            # ── SW golden model ──────────────────────────────────────
            sw_assigns = dict(sc["initial_assigns"])
            sw_impls, sw_conflict = sw_bcp_loop(
                clauses, watch_lists, sw_assigns, sc["initial_false_lit"]
            )

            # ── HW simulation ────────────────────────────────────────
            hw_impls, hw_conflict = await run_hw_bcp_loop(
                sc["initial_false_lit"]
            )

            # ── Compare ──────────────────────────────────────────────
            assert len(hw_impls) == len(sw_impls), (
                f"[{name}] implication count mismatch: "
                f"SW={len(sw_impls)} HW={len(hw_impls)}\n"
                f"  SW: {sw_impls}\n  HW: {hw_impls}"
            )

            for i, (sw, hw) in enumerate(zip(sw_impls, hw_impls)):
                assert sw["var"] == hw["var"], (
                    f"[{name}] impl {i} var mismatch: SW={sw['var']} HW={hw['var']}"
                )
                assert sw["value"] == hw["value"], (
                    f"[{name}] impl {i} value mismatch: SW={sw['value']} HW={hw['value']}"
                )
                assert sw["reason"] == hw["reason"], (
                    f"[{name}] impl {i} reason mismatch: SW={sw['reason']} HW={hw['reason']}"
                )

            assert sw_conflict == hw_conflict, (
                f"[{name}] conflict mismatch: SW={sw_conflict} HW={hw_conflict}"
            )

            tag = "CONFLICT" if sw_conflict is not None else "OK"
            print(
                f"  PASSED: {name}  "
                f"({len(sw_impls)} implication(s), {tag})"
            )

        print("\nAll end-to-end scenarios PASSED.  "
              "Hardware matches software golden model.")

    sim.add_testbench(testbench)

    with sim.write_vcd(os.path.join(os.path.dirname(__file__), "..", "..", "logs", "bcp_end_to_end.vcd")):
        sim.run()


if __name__ == "__main__":
    test_bcp_end_to_end()
