"""
Python CDCL SAT Solver — direct port of src/software/CDCL.c.

Implements two-watched-literal BCP, first-UIP conflict analysis,
non-chronological backtracking, and VSIDS decision heuristic.

BCP is pluggable: the solver can use software propagation or
delegate to a hardware BCP callback.
"""

from dataclasses import dataclass, field
from .dimacs_parser import CNFFormula, lit_to_code, lit_neg, lit_var

UNASSIGNED = -1
VSIDS_DECAY = 0.95


@dataclass
class SolverStats:
    decisions: int = 0
    conflicts: int = 0
    propagations: int = 0   # number of BCP rounds (prop_head advances)
    implications: int = 0   # total enqueued implications
    learned_clauses: int = 0


@dataclass
class Clause:
    size: int
    learnt: bool
    lits: list  # internal literal codes


class CDCLSolver:
    """CDCL solver matching the C implementation line-by-line."""

    def __init__(self, formula: CNFFormula):
        n = formula.num_vars
        self.num_vars = n
        self.stats = SolverStats()

        # Per-variable arrays (1-indexed, index 0 unused)
        self.assigns = [UNASSIGNED] * (n + 1)
        self.levels = [0] * (n + 1)
        self.reasons = [-1] * (n + 1)
        self.activity = [0.0] * (n + 1)

        # Propagation trail
        self.trail = []
        self.prop_head = 0
        self.trail_delimiters = []  # trail_size at start of each decision level
        self.num_decisions = 0

        # Watch lists: indexed by literal code
        num_lits = 2 * n + 2
        self.watches = [[] for _ in range(num_lits)]

        # Clause database
        self.clauses = []

        # VSIDS
        self.var_inc = 1.0

        # Add initial clauses from formula
        for signed_lits in formula.clauses:
            self._add_clause(signed_lits, learnt=False)

        # Pluggable BCP callback: async fn(solver) -> conflict_ci or -1
        # Set by hw_bcp_sim before solving
        self.hw_propagate = None

    def _add_clause(self, signed_lits, learnt=False):
        """Add a clause from signed literals. Returns clause index."""
        lits = [lit_to_code(l) for l in signed_lits]
        c = Clause(size=len(lits), learnt=learnt, lits=lits)
        ci = len(self.clauses)
        self.clauses.append(c)

        if len(lits) >= 2:
            self.watches[lits[0]].append(ci)
            self.watches[lits[1]].append(ci)

        return ci

    def _add_learnt_clause(self, lit_codes, learnt=True):
        """Add a learned clause from internal literal codes. Returns clause index."""
        c = Clause(size=len(lit_codes), learnt=learnt, lits=list(lit_codes))
        ci = len(self.clauses)
        self.clauses.append(c)

        if len(lit_codes) >= 2:
            self.watches[lit_codes[0]].append(ci)
            self.watches[lit_codes[1]].append(ci)

        if learnt:
            self.stats.learned_clauses += 1

        return ci

    # ── Assignment helpers ────────────────────────────────────────────────

    def _lit_value(self, code):
        """Current truth value of internal literal code."""
        var = lit_var(code)
        a = self.assigns[var]
        if a == UNASSIGNED:
            return UNASSIGNED
        # Even code (positive): value matches assignment
        # Odd code (negative): flip 0<->1
        if code & 1:
            return a ^ 1
        return a

    def _enqueue(self, code, reason):
        """Assign a literal at the current decision level."""
        var = lit_var(code)
        self.assigns[var] = 0 if (code & 1) else 1  # even->TRUE, odd->FALSE
        self.levels[var] = self.num_decisions
        self.reasons[var] = reason
        self.trail.append(code)
        self.stats.implications += 1

    # ── Software BCP ──────────────────────────────────────────────────────

    def _propagate_sw(self):
        """Software BCP using two-watched-literal scheme.
        Returns conflict clause index, or -1 if no conflict."""
        while self.prop_head < len(self.trail):
            false_lit = lit_neg(self.trail[self.prop_head])
            self.prop_head += 1
            self.stats.propagations += 1

            wlist = self.watches[false_lit]
            j = 0  # write pointer for compacting

            i = 0
            while i < len(wlist):
                ci = wlist[i]
                c = self.clauses[ci]

                # Make sure false_lit is in position 1
                if c.lits[0] == false_lit:
                    c.lits[0], c.lits[1] = c.lits[1], c.lits[0]

                # If other watched literal is true, clause is satisfied
                if self._lit_value(c.lits[0]) == 1:
                    wlist[j] = ci
                    j += 1
                    i += 1
                    continue

                # Try to find replacement watch literal
                found = False
                for k in range(2, c.size):
                    if self._lit_value(c.lits[k]) != 0:  # not false
                        c.lits[1], c.lits[k] = c.lits[k], c.lits[1]
                        self.watches[c.lits[1]].append(ci)
                        found = True
                        break

                if found:
                    i += 1
                    continue

                # No replacement — unit or conflict
                wlist[j] = ci
                j += 1

                if self._lit_value(c.lits[0]) == 0:
                    # CONFLICT: copy remaining watches
                    i += 1
                    while i < len(wlist):
                        wlist[j] = wlist[i]
                        j += 1
                        i += 1
                    del wlist[j:]
                    return ci

                # Unit clause
                self._enqueue(c.lits[0], ci)
                i += 1

            del wlist[j:]

        return -1  # no conflict

    # ── VSIDS ─────────────────────────────────────────────────────────────

    def _var_bump_activity(self, var):
        self.activity[var] += self.var_inc
        if self.activity[var] > 1e100:
            for v in range(1, self.num_vars + 1):
                self.activity[v] *= 1e-100
            self.var_inc *= 1e-100

    def _var_decay_activity(self):
        self.var_inc /= VSIDS_DECAY

    # ── Conflict analysis (first-UIP) ─────────────────────────────────────

    def _analyze(self, conflict_ci):
        """Analyze conflict, return (learnt_lits, bt_level)."""
        current_level = self.num_decisions
        seen = [False] * (self.num_vars + 1)

        learnt = []
        counter = 0

        # Start with conflict clause
        c = self.clauses[conflict_ci]
        for lit in c.lits:
            var = lit_var(lit)
            if not seen[var]:
                seen[var] = True
                self._var_bump_activity(var)
                if self.levels[var] == current_level:
                    counter += 1
                elif self.levels[var] > 0:
                    learnt.append(lit)

        # Walk trail backwards to first UIP
        trail_idx = len(self.trail) - 1
        uip_lit = 0

        while counter > 0:
            while not seen[lit_var(self.trail[trail_idx])]:
                trail_idx -= 1
            p = self.trail[trail_idx]
            trail_idx -= 1
            var = lit_var(p)
            seen[var] = False
            counter -= 1

            if counter == 0:
                uip_lit = lit_neg(p)
            else:
                reason_ci = self.reasons[var]
                assert reason_ci >= 0
                rc = self.clauses[reason_ci]
                for lit in rc.lits:
                    rvar = lit_var(lit)
                    if rvar == var:
                        continue  # skip the resolved variable
                    if not seen[rvar]:
                        seen[rvar] = True
                        self._var_bump_activity(rvar)
                        if self.levels[rvar] == current_level:
                            counter += 1
                        elif self.levels[rvar] > 0:
                            learnt.append(lit)

        # UIP literal goes first
        learnt.insert(0, uip_lit)

        # Determine backtrack level
        bt_level = 0
        max_idx = 1
        for i in range(1, len(learnt)):
            lv = self.levels[lit_var(learnt[i])]
            if lv > bt_level:
                bt_level = lv
                max_idx = i

        # Swap highest-level literal into position 1 for watching
        if len(learnt) > 1:
            learnt[1], learnt[max_idx] = learnt[max_idx], learnt[1]

        self._var_decay_activity()

        return learnt, bt_level

    # ── Backtracking ──────────────────────────────────────────────────────

    def _backtrack(self, level):
        """Undo assignments above the given decision level."""
        while len(self.trail) > 0:
            if self.num_decisions <= level:
                break

            if (self.num_decisions > level and
                    len(self.trail) <= self.trail_delimiters[self.num_decisions - 1]):
                self.num_decisions -= 1
                continue

            code = self.trail.pop()
            var = lit_var(code)
            self.assigns[var] = UNASSIGNED
            self.reasons[var] = -1

        while self.num_decisions > level:
            self.num_decisions -= 1

        # Trim trail_delimiters to match
        del self.trail_delimiters[self.num_decisions:]

        self.prop_head = len(self.trail)

    # ── Decision heuristic (VSIDS) ────────────────────────────────────────

    def _pick_decision_var(self):
        """Pick unassigned variable with highest activity. Returns 0 if all assigned."""
        best_var = 0
        best_act = -1.0
        for v in range(1, self.num_vars + 1):
            if self.assigns[v] == UNASSIGNED and self.activity[v] > best_act:
                best_act = self.activity[v]
                best_var = v
        return best_var

    # ── Main solve loop ───────────────────────────────────────────────────

    def solve(self):
        """Run CDCL solver. Returns (is_sat: bool, stats: SolverStats)."""
        # Handle initial unit clauses
        for i, c in enumerate(self.clauses):
            if c.size == 0:
                return False, self.stats
            if c.size == 1:
                if self._lit_value(c.lits[0]) == 0:
                    return False, self.stats
                if self._lit_value(c.lits[0]) == UNASSIGNED:
                    self._enqueue(c.lits[0], i)

        while True:
            conflict = self._propagate_sw()

            if conflict >= 0:
                # CONFLICT
                self.stats.conflicts += 1
                if self.num_decisions == 0:
                    return False, self.stats

                learnt_lits, bt_level = self._analyze(conflict)
                self._backtrack(bt_level)

                if len(learnt_lits) == 1:
                    self._enqueue(learnt_lits[0], -1)
                else:
                    ci = self._add_learnt_clause(learnt_lits)
                    self._enqueue(learnt_lits[0], ci)
            else:
                # NO CONFLICT — make a decision
                dec_var = self._pick_decision_var()
                if dec_var == 0:
                    return True, self.stats

                self.stats.decisions += 1
                self.trail_delimiters.append(len(self.trail))
                self.num_decisions += 1

                # Decide: assign FALSE (matches C solver)
                dec_lit = lit_to_code(-dec_var)
                self._enqueue(dec_lit, -1)
