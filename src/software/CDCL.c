/*
 * CDCL.c — Conflict-Driven Clause Learning SAT Solver
 *
 * A standard CDCL implementation following the modern architecture:
 *   1. Unit propagation (BCP) with a two-watched-literal scheme
 *   2. VSIDS-style decision heuristic
 *   3. First-UIP conflict analysis with clause learning
 *   4. Non-chronological backtracking
 *
 * CNF formulas are provided in a simple internal representation.
 * Variables are numbered 1..n. Literals use the mapping:
 *   positive literal x  ->  2*x
 *   negative literal ~x ->  2*x + 1
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <assert.h>

#include "CDCL.h"

/* ========================================================================= */
/*  Utility helpers                                                          */
/* ========================================================================= */

/* Convert a signed literal (1-based, negative = negated) to internal code. */
static inline int lit_to_code(int lit) {
    return (lit > 0) ? (2 * lit) : (2 * (-lit) + 1);
}

/* Return the variable index for an internal literal code. */
static inline int lit_var(int code) {
    return code / 2;
}

/* Return the negation of an internal literal code. Does this by flipping least significant bit (+1/-1). */
static inline int lit_neg(int code) {
    return code ^ 1;
}

/* ========================================================================= */
/*  Solver creation / destruction                                            */
/* ========================================================================= */

CDCLSolver *cdcl_create(int num_vars) {
    CDCLSolver *s = (CDCLSolver *)calloc(1, sizeof(CDCLSolver));
    // calloc zero-initializes.
    s->num_vars = num_vars;
    /* Internal literal codes range from 2..2*num_vars+1.  Allocate 2*n+2. */
    int lits = 2 * num_vars + 2;

    /* Variable-indexed arrays (index 0 unused). */
    // Store assignment, decision level, reason (Clause that implied 'v'), and activity (VSIDS) for each variable (1-indexed).
    s->assigns    = (int *)malloc((num_vars + 1) * sizeof(int));
    s->levels     = (int *)malloc((num_vars + 1) * sizeof(int));
    s->reasons    = (int *)malloc((num_vars + 1) * sizeof(int));
    s->activity   = (double *)calloc(num_vars + 1, sizeof(double));
    memset(s->assigns, 0xFF, (num_vars + 1) * sizeof(int)); /* UNASSIGNED = -1 (Two's complement: 0xFF)*/
    memset(s->levels, 0, (num_vars + 1) * sizeof(int));
    for (int i = 0; i <= num_vars; i++) s->reasons[i] = -1;

    /* Propagation trail. */
    s->trail      = (int *)malloc((num_vars + 1) * sizeof(int));
    s->trail_size = 0;
    s->trail_delimiters = (int *)malloc((num_vars + 1) * sizeof(int));
    s->num_decisions    = 0;

    /* Watched-literal lists: one list per literal code. */
    s->watch_cap  = (int *)calloc(lits, sizeof(int));
    s->watch_size = (int *)calloc(lits, sizeof(int));
    s->watches    = (int **)calloc(lits, sizeof(int *));

    /* Clause database — start with room for 1024 clauses. */
    s->clause_cap = 1024;
    s->clause_count = 0;
    s->clauses = (Clause **)calloc(s->clause_cap, sizeof(Clause *));

    /* VSIDS decay factor. (Baseline Conflict Bump) */
    s->var_inc = 1.0;

    return s;
}

void cdcl_destroy(CDCLSolver *s) {
    int lits = 2 * s->num_vars + 2;
    for (int i = 0; i < lits; i++) free(s->watches[i]);
    free(s->watches);
    free(s->watch_cap);
    free(s->watch_size);
    for (int i = 0; i < s->clause_count; i++) free(s->clauses[i]);
    free(s->clauses);
    free(s->trail);
    free(s->trail_delimiters);
    free(s->assigns);
    free(s->levels);
    free(s->reasons);
    free(s->activity);
    free(s);
}

/* ========================================================================= */
/*  Watched-literal helpers                                                  */
/* ========================================================================= */

/* Add clause index `ci` to the watch list of literal `lit`. */
static void watch_add(CDCLSolver *s, int lit, int ci) {
    // Dynamically grows the watch list if needed. Starts w/ capacity 4 and doubles as needed.
    if (s->watch_size[lit] == s->watch_cap[lit]) {
        s->watch_cap[lit] = s->watch_cap[lit] ? s->watch_cap[lit] * 2 : 4;
        // reallocates, preserving existing contents.
        s->watches[lit] = (int *)realloc(s->watches[lit],
                                         s->watch_cap[lit] * sizeof(int));
    }
    // Add the clause index to the watch list and increment the size.
    s->watches[lit][s->watch_size[lit]++] = ci;
}

/* ========================================================================= */
/*  Clause addition                                                          */
/* ========================================================================= */

/*
 * Add a clause given as an array of signed literals (1-based, negated = negative).
 * Returns the clause index, or -1 if the clause is a tautology / empty.
 */
int cdcl_add_clause(CDCLSolver *s, int *signed_lits, int len) {
    /* Grow clause database if necessary. */
    if (s->clause_count == s->clause_cap) {
        s->clause_cap *= 2;
        s->clauses = (Clause **)realloc(s->clauses,
                                        s->clause_cap * sizeof(Clause *));
    }

    /* Allocate and populate clause. */
    Clause *c = (Clause *)malloc(sizeof(Clause) + len * sizeof(int));
    c->size = len;
    c->learnt = false;
    for (int i = 0; i < len; i++) {
        c->lits[i] = lit_to_code(signed_lits[i]);
    }

    int ci = s->clause_count;
    s->clauses[ci] = c;
    s->clause_count++;

    /* Set up watched literals: watch the first two literals (if >= 2). */
    if (len >= 2) {
        watch_add(s, c->lits[0], ci);
        watch_add(s, c->lits[1], ci);
    }

    return ci;
}

/* ========================================================================= */
/*  Assignment / trail management                                            */
/* ========================================================================= */

/* Return the current truth value of an internal literal code. */
static inline int lit_value(CDCLSolver *s, int code) {
    int var = lit_var(code);
    if (s->assigns[var] == UNASSIGNED) return UNASSIGNED;
    /* Positive literal (even code): value matches assignment.
       Negative literal (odd code): value is flipped.
       Returns 0 for FALSE, 1 for TRUE, -1 for UNASSIGNED */
    if (code & 1)
        return s->assigns[var] ^ 1; /* flip 0<->1 */
    else
        return s->assigns[var];
}

/* Enqueue a literal assignment at the current decision level.
 * `reason` is the clause index that implied this assignment, or -1 for decisions. */
static void enqueue(CDCLSolver *s, int code, int reason) {
    int var = lit_var(code);
    s->assigns[var] = (code & 1) ? 0 : 1;  /* even code -> TRUE, odd -> FALSE */
    s->levels[var]  = s->num_decisions;
    s->reasons[var] = reason;
    s->trail[s->trail_size++] = code;
}

/* ========================================================================= */
/*  Boolean Constraint Propagation (BCP)                                     */
/* ========================================================================= */

/*
 * Perform unit propagation using two-watched-literal scheme.
 * Returns -1 if no conflict, otherwise returns the index of a conflicting clause.
 */
static int propagate(CDCLSolver *s) {
    /* Process from the current propagation pointer to the end of the trail. */
    while (s->prop_head < s->trail_size) {
        /* The literal that just became true; we need to look at watchers of
         * its negation (those clauses might now be unit or conflicting). */
        int false_lit = lit_neg(s->trail[s->prop_head++]);

        int *wlist = s->watches[false_lit];
        int  wlen  = s->watch_size[false_lit];
        // Optimization: Defer Watch List Update to after processing all clauses (Removes Loop Dependency). Source: FYalSAT (Choi & Kim, 2024) — Section III-B, deferred break score aggregation as a general technique for decoupling dependent writes from parallel reads.
        int  j = 0; /* write pointer for compacting the watch list */

        // Optimization: Parallelization (Unroll Loop Completely). Source: SAT-Accel (Lo et al., 2025) — Section IV-B, multiple BCP Processing Elements (PEs) operating in parallel on different clauses
        // Optimization: Conflict-Free Partitioning of Watch Lists to avoid bank access conflicts. Source: FYalSAT (Choi & Kim, 2024) — Section III-A, modulo-P conflict-free occurrence list rearrangement.
        for (int i = 0; i < wlen; i++) {
            // Pipeline 1 and 2 by prefetching the next clause index while the current clause is being processed. Source: FYalSAT (Choi & Kim, 2024) — Section III-C, unsatisfied clause prefetching to overlap DRAM access with computation.
            int ci = wlist[i]; // 1
            Clause *c = s->clauses[ci]; //2 

            /* Make sure the false literal is in position 1. Always check first literal and swap the two literals.
            (Simplifies logic so we never need to iterate over the clause) */
            // Optimization: Remove Swap and utilize hardware multiplexer to select the other watched literal: Source: SAT-Accel (Lo et al., 2025) — Section V, signature-based clause representation eliminates positional literal dependency entirely, removing the need for this normalization.
            if (c->lits[0] == false_lit) {
                int tmp = c->lits[0];
                c->lits[0] = c->lits[1];
                c->lits[1] = tmp;
            }

            /* If the other watched literal is already true, clause is satisfied. */
            // Optimization: Remove in favor of a satisfaction bit we store with the clause in memory to avoid checking literal value. Source: FYalSAT (Choi & Kim, 2024) — Section IV-B, Partial SAT Evaluator module (Stage C) using precomputed satisfaction status per clause.
            if (lit_value(s, c->lits[0]) == 1) {
                wlist[j++] = ci; /* keep watching */
                continue;
            }

            /* Try to find a new literal to watch in place of lits[1]. */
            // Optimization Option 1: Compute each in parallel (Unroll Loop Fully). Source: FYalSAT (Choi & Kim, 2024) — Section IV-B3, Sub Clause Evaluator units evaluating all literals in a clause simultaneously.
            // Optimization Option 2: Use state-based approach (ucnt + XOR signature) eliminates this search entirely. Source: SAT-Accel (Lo et al., 2025) — Section V-A, signature-based clause representation with ucnt and XOR of unassigned variable indices.
            bool found = false;
            for (int k = 2; k < c->size; k++) {
                if (lit_value(s, c->lits[k]) != 0) { /* not false */
                    /* Swap lits[1] and lits[k]. */
                    int tmp = c->lits[1];
                    c->lits[1] = c->lits[k];
                    c->lits[k] = tmp;
                    watch_add(s, c->lits[1], ci);
                    found = true;
                    break;
                }
            }
            if (found) continue; /* don't keep in this watch list */

            /* No replacement found — clause is either unit or conflicting. */
            // Optimization: Defer Watch List Update to after processing all clauses (Removes Loop Dependency). Source: FYalSAT (Choi & Kim, 2024) — Section III-B, deferred break score aggregation as a general technique for decoupling dependent writes from parallel reads.
            wlist[j++] = ci;

            if (lit_value(s, c->lits[0]) == 0) {
                /* CONFLICT: all literals are false. */
                /* Copy remaining watches and update size. */
                // Optimization: priority-encoded reduction — all clauses evaluate simultaneously, and a conflict anywhere triggers a single combined result without sequential drain. Source: SAT-Accel (Lo et al., 2025) — Section IV-B, conflict detection unit operating across all parallel processing elements with priority encoding.  
                while (i + 1 < wlen) {
                    wlist[j++] = wlist[++i];
                }
                // Optimization: Defer Watch List Update to after processing all clauses (Removes Loop Dependency). Source: FYalSAT (Choi & Kim, 2024) — Section III-B, deferred break score aggregation as a general technique for decoupling dependent writes from parallel reads.
                s->watch_size[false_lit] = j;
                return ci;
            }

            /* Unit clause: lits[0] is the only unassigned literal. */
            // Optimization: Implement as FIFO in hardware to pipeline with (Prefetch, Evaluation, Enqueue). Source: SAT-Accel (Lo et al., 2025) — Section IV-B, pipelined BCP with overlapped implication propagation and clause evaluation.
            enqueue(s, c->lits[0], ci);
        }

        s->watch_size[false_lit] = j;
    }
    return -1; /* no conflict */
}

/* ========================================================================= */
/*  VSIDS Activity                                                           */
/* ========================================================================= */

#define VSIDS_DECAY 0.95

/* Bump the activity score of a variable (called during conflict analysis). */
static void var_bump_activity(CDCLSolver *s, int var) {
    s->activity[var] += s->var_inc;
    /* Rescale if activity gets too large to prevent overflow. */
    if (s->activity[var] > 1e100) {
        for (int i = 1; i <= s->num_vars; i++)
            s->activity[i] *= 1e-100;
        s->var_inc *= 1e-100;
    }
}

/* Decay all activities (called once per conflict). */
static void var_decay_activity(CDCLSolver *s) {
    s->var_inc /= VSIDS_DECAY;
}

/* ========================================================================= */
/*  Conflict analysis — First-UIP scheme                                     */
/* ========================================================================= */

/*
 * Analyze a conflict clause and produce a learned clause.
 * Sets `out_bt_level` to the backtrack level.
 * Returns the number of literals in the learned clause stored in `learnt_buf`.
 */
static int analyze(CDCLSolver *s, int conflict_ci,
                   int *learnt_buf, int *out_bt_level) {
    int current_level = s->num_decisions;
    bool *seen = (bool *)calloc(s->num_vars + 1, sizeof(bool));

    int learnt_count = 0;
    int counter = 0; /* number of literals at current decision level still to resolve */

    /* Start with the conflict clause. */
    Clause *c = s->clauses[conflict_ci];
    for (int i = 0; i < c->size; i++) {
        int var = lit_var(c->lits[i]);
        if (!seen[var]) {
            seen[var] = true;
            var_bump_activity(s, var);
            if (s->levels[var] == current_level) {
                counter++;
            } else if (s->levels[var] > 0) {
                learnt_buf[learnt_count++] = c->lits[i];
            }
        }
    }

    /* Walk the trail backwards, resolving until we reach the first UIP. */
    int trail_idx = s->trail_size - 1;
    int uip_lit = 0;

    while (counter > 0) {
        /* Find the next literal on the trail that was seen. */
        while (!seen[lit_var(s->trail[trail_idx])]) trail_idx--;
        int p = s->trail[trail_idx--];
        int var = lit_var(p);
        seen[var] = false;
        counter--;

        if (counter == 0) {
            /* This is the first UIP — negate it for the learned clause. */
            uip_lit = lit_neg(p);
        } else {
            /* Resolve with the reason clause. */
            int reason_ci = s->reasons[var];
            assert(reason_ci >= 0);
            Clause *rc = s->clauses[reason_ci];
            for (int i = 0; i < rc->size; i++) {
                int rvar = lit_var(rc->lits[i]);
                if (!seen[rvar]) {
                    seen[rvar] = true;
                    var_bump_activity(s, rvar);
                    if (s->levels[rvar] == current_level) {
                        counter++;
                    } else if (s->levels[rvar] > 0) {
                        learnt_buf[learnt_count++] = rc->lits[i];
                    }
                }
            }
        }
    }

    /* The UIP literal goes first in the learned clause. */
    /* Shift existing learnt literals right to make room at index 0. */
    for (int i = learnt_count; i > 0; i--)
        learnt_buf[i] = learnt_buf[i - 1];
    learnt_buf[0] = uip_lit;
    learnt_count++;

    /* Determine the backtrack level: the highest level among the non-UIP
     * literals in the learned clause (or 0 if the clause is unit). */
    int bt_level = 0;
    int max_idx = 1; /* index of literal with the highest level (for watch) */
    for (int i = 1; i < learnt_count; i++) {
        int lv = s->levels[lit_var(learnt_buf[i])];
        if (lv > bt_level) {
            bt_level = lv;
            max_idx = i;
        }
    }
    /* Swap the highest-level literal into position 1 for watching. */
    if (learnt_count > 1) {
        int tmp = learnt_buf[1];
        learnt_buf[1] = learnt_buf[max_idx];
        learnt_buf[max_idx] = tmp;
    }

    *out_bt_level = bt_level;
    free(seen);

    var_decay_activity(s);

    return learnt_count;
}

/* ========================================================================= */
/*  Backtracking                                                             */
/* ========================================================================= */

/* Undo all assignments above the given decision level. */
static void backtrack(CDCLSolver *s, int level) {
    while (s->trail_size > 0) {
        /* If we've unwound past the target level, stop. */
        if (s->num_decisions <= level) break;

        /* Check if the top of the trail is a decision boundary. */
        if (s->num_decisions > level &&
            s->trail_size <= s->trail_delimiters[s->num_decisions - 1]) {
            s->num_decisions--;
            continue;
        }

        int code = s->trail[--s->trail_size];
        int var = lit_var(code);
        s->assigns[var] = UNASSIGNED;
        s->reasons[var] = -1;
    }
    /* Also pop any remaining decision-level markers. */
    while (s->num_decisions > level) {
        s->num_decisions--;
    }
    /* Reset the propagation pointer so BCP re-processes from the new trail end. */
    s->prop_head = s->trail_size;
}

/* ========================================================================= */
/*  Decision heuristic (VSIDS)                                               */
/* ========================================================================= */

/* Pick the unassigned variable with the highest activity score.
 * Returns 0 if all variables are assigned (SAT). */
static int pick_decision_var(CDCLSolver *s) {
    int best_var = 0;
    double best_act = -1.0;
    for (int v = 1; v <= s->num_vars; v++) {
        if (s->assigns[v] == UNASSIGNED && s->activity[v] > best_act) {
            best_act = s->activity[v];
            best_var = v;
        }
    }
    return best_var;
}

/* ========================================================================= */
/*  Add a learned clause to the database                                     */
/* ========================================================================= */

static int add_learnt_clause(CDCLSolver *s, int *lits, int len) {
    if (s->clause_count == s->clause_cap) {
        s->clause_cap *= 2;
        s->clauses = (Clause **)realloc(s->clauses,
                                        s->clause_cap * sizeof(Clause *));
    }
    Clause *c = (Clause *)malloc(sizeof(Clause) + len * sizeof(int));
    c->size = len;
    c->learnt = true;
    memcpy(c->lits, lits, len * sizeof(int));

    int ci = s->clause_count;
    s->clauses[ci] = c;
    s->clause_count++;

    if (len >= 2) {
        watch_add(s, c->lits[0], ci);
        watch_add(s, c->lits[1], ci);
    }
    return ci;
}

/* ========================================================================= */
/*  Top-level solve loop                                                     */
/* ========================================================================= */

/*
 * Main CDCL solving routine.
 * Returns SAT (1) or UNSAT (0).
 * If SAT, the satisfying assignment is available via s->assigns[].
 */
int cdcl_solve(CDCLSolver *s) {
    /* Handle any unit clauses present at the start. */
    for (int i = 0; i < s->clause_count; i++) {
        Clause *c = s->clauses[i];
        if (c->size == 0) return UNSAT;
        if (c->size == 1) {
            if (lit_value(s, c->lits[0]) == 0) return UNSAT; /* contradictory unit */
            if (lit_value(s, c->lits[0]) == UNASSIGNED)
                enqueue(s, c->lits[0], i);
        }
    }

    /* Buffer for learned clauses (max possible size = num_vars). */
    int *learnt_buf = (int *)malloc((s->num_vars + 1) * sizeof(int));

    while (true) {
        int conflict = propagate(s);

        if (conflict >= 0) {
            /* CONFLICT */
            if (s->num_decisions == 0) {
                /* Conflict at decision level 0 — formula is UNSAT. */
                free(learnt_buf);
                return UNSAT;
            }

            /* Analyze the conflict and derive a learned clause. */
            int bt_level = 0;
            int learnt_len = analyze(s, conflict, learnt_buf, &bt_level);

            /* Backtrack to the computed level. */
            backtrack(s, bt_level);

            /* Add the learned clause and propagate the asserting literal. */
            if (learnt_len == 1) {
                /* Unit learned clause — enqueue at level 0. */
                enqueue(s, learnt_buf[0], -1);
            } else {
                int ci = add_learnt_clause(s, learnt_buf, learnt_len);
                enqueue(s, learnt_buf[0], ci);
            }
        } else {
            /* NO CONFLICT — make a decision. */
            int dec_var = pick_decision_var(s);
            if (dec_var == 0) {
                /* All variables assigned — formula is SAT. */
                free(learnt_buf);
                return SAT;
            }

            /* New decision level. */
            s->trail_delimiters[s->num_decisions] = s->trail_size;
            s->num_decisions++;

            /* Decide: assign the variable to FALSE (arbitrary polarity). */
            int dec_lit = lit_to_code(-dec_var); /* negative literal = assign FALSE */
            enqueue(s, dec_lit, -1);
        }
    }
}

/* ========================================================================= */
/*  Query the satisfying assignment                                          */
/* ========================================================================= */

int cdcl_get_value(CDCLSolver *s, int var) {
    if (var < 1 || var > s->num_vars) return UNASSIGNED;
    return s->assigns[var];
}
