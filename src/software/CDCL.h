/*
 * CDCL.h — Public interface for the CDCL SAT Solver
 *
 * Usage:
 *   1. Create a solver with cdcl_create(num_vars).
 *   2. Add clauses with cdcl_add_clause() using signed literals
 *      (positive = variable true, negative = variable false).
 *   3. Call cdcl_solve() — returns SAT or UNSAT.
 *   4. If SAT, query variable values with cdcl_get_value().
 *   5. Free with cdcl_destroy().
 */

#ifndef CDCL_H
#define CDCL_H

#include <stdbool.h>

/* Solver return values. */
#define SAT        1
#define UNSAT      0
#define UNASSIGNED (-1)

/* ========================================================================= */
/*  Data structures                                                          */
/* ========================================================================= */

/*
 * Clause: a disjunction of literals.
 * Stored with a flexible array member for the literal list.
 * Literals use the internal encoding: positive x -> 2*x, negative x -> 2*x+1.
 */
typedef struct {
    int  size;      /* number of literals                     */
    bool learnt;    /* true if this clause was learned         */
    int  lits[];    /* flexible array of internal literal codes */
} Clause;

/*
 * CDCLSolver: the main solver state.
 */
typedef struct {
    int num_vars;           /* number of variables (1-indexed)       */

    /* Per-variable data (indexed 1..num_vars). */
    int    *assigns;        /* current assignment: 0=FALSE, 1=TRUE, -1=UNASSIGNED */
    int    *levels;         /* decision level at which variable was assigned       */
    int    *reasons;        /* clause index that implied the assignment, or -1     */
    double *activity;       /* VSIDS activity score                               */

    /* Propagation trail. */
    int *trail;             /* sequence of assigned literal codes      */
    int  trail_size;        /* current length of the trail             */
    int  prop_head;         /* propagation queue head pointer          */
    int *trail_delimiters;  /* trail_size at the start of each decision level */
    int  num_decisions;     /* current decision level                  */

    /* Two-watched-literal scheme: one watch list per literal code. */
    int **watches;          /* watches[lit] = array of clause indices  */
    int  *watch_size;       /* current size of each watch list         */
    int  *watch_cap;        /* allocated capacity of each watch list   */

    /* Clause database. */
    Clause **clauses;       /* array of clause pointers    */
    int      clause_count;  /* number of clauses           */
    int      clause_cap;    /* allocated capacity          */

    /* VSIDS increment (grows on each decay). */
    double var_inc;
} CDCLSolver;

/* ========================================================================= */
/*  Public API                                                               */
/* ========================================================================= */

/* Create a new solver for a problem with `num_vars` variables. */
CDCLSolver *cdcl_create(int num_vars);

/* Free all memory associated with the solver. */
void cdcl_destroy(CDCLSolver *s);

/*
 * Add a clause to the formula.
 * `signed_lits` is an array of signed integers: positive = var, negative = ~var.
 * `len` is the number of literals.
 * Returns the clause index, or -1 on error.
 */
int cdcl_add_clause(CDCLSolver *s, int *signed_lits, int len);

/*
 * Solve the formula.
 * Returns SAT (1) if satisfiable, UNSAT (0) if unsatisfiable.
 */
int cdcl_solve(CDCLSolver *s);

/*
 * After a SAT result, query the value of a variable (1-indexed).
 * Returns 0 (FALSE), 1 (TRUE), or UNASSIGNED (-1).
 */
int cdcl_get_value(CDCLSolver *s, int var);

#endif /* CDCL_H */
