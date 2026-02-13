/*
 * test_CDCL.c — Simple testbench for the CDCL SAT Solver
 *
 * Runs a series of small CNF instances and checks results against
 * expected SAT/UNSAT outcomes. For SAT instances, verifies that the
 * returned assignment actually satisfies every clause.
 *
 * Compile:
 *   gcc -O2 -I../../src/software -o test_CDCL \
 *       test_CDCL.c ../../src/software/CDCL.c -lm
 *
 * Run:
 *   ./test_CDCL
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "CDCL.h"

/* ========================================================================= */
/*  Test helpers                                                             */
/* ========================================================================= */

static int tests_run    = 0;
static int tests_passed = 0;

/*
 * Verify that a satisfying assignment actually satisfies every clause.
 * `clauses` is an array of int arrays (NULL-terminated), each clause
 * is a signed-literal array terminated by 0.
 */
static int verify_assignment(CDCLSolver *s, int clauses[][10], int num_clauses) {
    for (int i = 0; i < num_clauses; i++) {
        int satisfied = 0;
        for (int j = 0; clauses[i][j] != 0; j++) {
            int lit = clauses[i][j];
            int var = (lit > 0) ? lit : -lit;
            int val = cdcl_get_value(s, var);
            /* lit > 0 means we need var = TRUE (1),
               lit < 0 means we need var = FALSE (0). */
            if ((lit > 0 && val == 1) || (lit < 0 && val == 0)) {
                satisfied = 1;
                break;
            }
        }
        if (!satisfied) {
            printf("    FAILED: clause %d not satisfied\n", i);
            return 0;
        }
    }
    return 1;
}

/* Print pass/fail and update counters. */
static void check(const char *name, int passed) {
    tests_run++;
    if (passed) {
        tests_passed++;
        printf("  [PASS] %s\n", name);
    } else {
        printf("  [FAIL] %s\n", name);
    }
}

/* ========================================================================= */
/*  Test cases                                                               */
/* ========================================================================= */

/*
 * Test 1: Simple satisfiable formula
 *   (x1 OR x2) AND (~x1 OR x3) AND (~x2 OR ~x3)
 *   3 variables, 3 clauses — easily satisfiable.
 */
static void test_simple_sat(void) {
    CDCLSolver *s = cdcl_create(3);

    int c1[] = { 1,  2};     /* x1 OR x2       */
    int c2[] = {-1,  3};     /* ~x1 OR x3      */
    int c3[] = {-2, -3};     /* ~x2 OR ~x3     */

    cdcl_add_clause(s, c1, 2);
    cdcl_add_clause(s, c2, 2);
    cdcl_add_clause(s, c3, 2);

    int result = cdcl_solve(s);
    check("simple SAT (result)", result == SAT);

    if (result == SAT) {
        int clauses[][10] = {{1,2,0}, {-1,3,0}, {-2,-3,0}};
        check("simple SAT (verify)", verify_assignment(s, clauses, 3));
    }

    cdcl_destroy(s);
}

/*
 * Test 2: Simple unsatisfiable formula
 *   (x1) AND (~x1)
 *   1 variable, 2 contradictory unit clauses.
 */
static void test_simple_unsat(void) {
    CDCLSolver *s = cdcl_create(1);

    int c1[] = { 1};
    int c2[] = {-1};

    cdcl_add_clause(s, c1, 1);
    cdcl_add_clause(s, c2, 1);

    int result = cdcl_solve(s);
    check("simple UNSAT", result == UNSAT);

    cdcl_destroy(s);
}

/*
 * Test 3: Single unit clause
 *   (x1)
 *   Trivially satisfiable — x1 = TRUE.
 */
static void test_unit_clause(void) {
    CDCLSolver *s = cdcl_create(1);

    int c1[] = {1};
    cdcl_add_clause(s, c1, 1);

    int result = cdcl_solve(s);
    check("unit clause SAT", result == SAT);
    check("unit clause value", cdcl_get_value(s, 1) == 1);

    cdcl_destroy(s);
}

/*
 * Test 4: Pigeonhole PHP(2,1) — 2 pigeons, 1 hole.
 *   x1 = "pigeon 1 in hole 1", x2 = "pigeon 2 in hole 1".
 *   At-least-one: (x1), (x2).
 *   At-most-one:  (~x1 OR ~x2).
 *   Cannot place 2 pigeons in 1 hole — UNSAT.
 */
static void test_pigeonhole_unsat(void) {
    CDCLSolver *s = cdcl_create(2);

    int c1[] = { 1};
    int c2[] = { 2};
    int c3[] = {-1, -2};

    cdcl_add_clause(s, c1, 1);
    cdcl_add_clause(s, c2, 1);
    cdcl_add_clause(s, c3, 2);

    int result = cdcl_solve(s);
    check("pigeonhole PHP(2,1) UNSAT", result == UNSAT);

    cdcl_destroy(s);
}

/*
 * Test 5: Larger satisfiable instance (XOR-like chain)
 *   Encodes x1 XOR x2, x2 XOR x3, x3 XOR x4  (all true).
 *   Each XOR(a,b) = (a OR b) AND (~a OR ~b).
 *   Satisfiable, e.g., x1=T, x2=F, x3=T, x4=F.
 */
static void test_xor_chain_sat(void) {
    CDCLSolver *s = cdcl_create(4);

    /* x1 XOR x2 */
    int c1[] = { 1,  2};  int c2[] = {-1, -2};
    /* x2 XOR x3 */
    int c3[] = { 2,  3};  int c4[] = {-2, -3};
    /* x3 XOR x4 */
    int c5[] = { 3,  4};  int c6[] = {-3, -4};

    cdcl_add_clause(s, c1, 2);
    cdcl_add_clause(s, c2, 2);
    cdcl_add_clause(s, c3, 2);
    cdcl_add_clause(s, c4, 2);
    cdcl_add_clause(s, c5, 2);
    cdcl_add_clause(s, c6, 2);

    int result = cdcl_solve(s);
    check("XOR chain SAT (result)", result == SAT);

    if (result == SAT) {
        int clauses[][10] = {
            {1,2,0}, {-1,-2,0},
            {2,3,0}, {-2,-3,0},
            {3,4,0}, {-3,-4,0}
        };
        check("XOR chain SAT (verify)", verify_assignment(s, clauses, 6));
    }

    cdcl_destroy(s);
}

/*
 * Test 6: All-positive 3-SAT (random-style, satisfiable)
 *   5 variables, several clauses — designed to be satisfiable with all TRUE.
 */
static void test_3sat(void) {
    CDCLSolver *s = cdcl_create(5);

    int c1[] = {1, 2, 3};
    int c2[] = {-1, 4, 5};
    int c3[] = {2, -4, 5};
    int c4[] = {-3, 4, -5};
    int c5[] = {1, -2, 5};

    cdcl_add_clause(s, c1, 3);
    cdcl_add_clause(s, c2, 3);
    cdcl_add_clause(s, c3, 3);
    cdcl_add_clause(s, c4, 3);
    cdcl_add_clause(s, c5, 3);

    int result = cdcl_solve(s);
    check("3-SAT instance (result)", result == SAT);

    if (result == SAT) {
        int clauses[][10] = {
            {1,2,3,0}, {-1,4,5,0}, {2,-4,5,0}, {-3,4,-5,0}, {1,-2,5,0}
        };
        check("3-SAT instance (verify)", verify_assignment(s, clauses, 5));
    }

    cdcl_destroy(s);
}

/*
 * Test 7: Empty clause (trivially UNSAT)
 */
static void test_empty_clause(void) {
    CDCLSolver *s = cdcl_create(2);

    int c1[] = {1, 2};
    cdcl_add_clause(s, c1, 2);
    cdcl_add_clause(s, NULL, 0); /* empty clause */

    int result = cdcl_solve(s);
    check("empty clause UNSAT", result == UNSAT);

    cdcl_destroy(s);
}

/* ========================================================================= */
/*  Main — run all tests                                                     */
/* ========================================================================= */

int main(void) {
    printf("=== CDCL SAT Solver Testbench ===\n\n");

    test_simple_sat();
    test_simple_unsat();
    test_unit_clause();
    test_pigeonhole_unsat();
    test_xor_chain_sat();
    test_3sat();
    test_empty_clause();

    printf("\n=== Results: %d / %d tests passed ===\n", tests_passed, tests_run);

    return (tests_passed == tests_run) ? 0 : 1;
}
