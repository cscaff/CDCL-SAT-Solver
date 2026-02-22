/*
 * main.c — DIMACS CNF Reader and SAT Solver Runner
 *
 * Reads a CNF formula in DIMACS format from a file, creates a CDCL solver,
 * adds all clauses, runs the solver, and prints the result.
 *
 * Usage:
 *   ./sat_solver [-p /dev/cu.usbserial-XXX] <file.cnf>
 *
 * The -p flag is only relevant when compiled with -DUSE_HW_BCP.
 *
 * DIMACS format:
 *   c comment lines (ignored)
 *   p cnf <num_vars> <num_clauses>
 *   1 -2 3 0        <- clause (x1 v ~x2 v x3), terminated by 0
 *   -1 2 0          <- clause (~x1 v x2)
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "CDCL.h"

#ifdef USE_HW_BCP
#include "hw_interface.h"
#endif

static void usage(const char *prog) {
    fprintf(stderr, "Usage: %s [-p port] <file.cnf>\n", prog);
    fprintf(stderr, "  -p port   Serial port for FPGA hardware BCP (requires USE_HW_BCP build)\n");
    exit(1);
}

int main(int argc, char *argv[]) {
    const char *port = NULL;
    const char *filename = NULL;

    /* Parse arguments */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-p") == 0) {
            if (i + 1 >= argc) usage(argv[0]);
            port = argv[++i];
        } else if (argv[i][0] == '-') {
            usage(argv[0]);
        } else {
            filename = argv[i];
        }
    }

    if (!filename) usage(argv[0]);

#ifdef USE_HW_BCP
    hw_port = port;
#else
    if (port) {
        fprintf(stderr, "Warning: -p flag ignored (not compiled with USE_HW_BCP)\n");
    }
#endif

    /* Open the CNF file */
    FILE *fp = fopen(filename, "r");
    if (!fp) {
        perror(filename);
        return 1;
    }

    int num_vars = 0, num_clauses = 0;
    int header_found = 0;
    char line[4096];

    /* Parse header */
    while (fgets(line, sizeof(line), fp)) {
        if (line[0] == 'c' || line[0] == '\n' || line[0] == '\r')
            continue;
        if (line[0] == 'p') {
            if (sscanf(line, "p cnf %d %d", &num_vars, &num_clauses) != 2) {
                fprintf(stderr, "Error: malformed p-line: %s", line);
                fclose(fp);
                return 1;
            }
            header_found = 1;
            break;
        }
    }

    if (!header_found) {
        fprintf(stderr, "Error: no 'p cnf ...' header found\n");
        fclose(fp);
        return 1;
    }

#ifdef USE_HW_BCP
    if (num_vars > 512)
        fprintf(stderr, "Warning: %d variables exceeds hardware limit (512)\n", num_vars);
    if (num_clauses > 8192)
        fprintf(stderr, "Warning: %d clauses exceeds hardware limit (8192)\n", num_clauses);
#endif

    /* Create solver */
    CDCLSolver *s = cdcl_create(num_vars);

    /* Parse clauses */
    int *lits = (int *)malloc(num_vars * sizeof(int));
    int lit_count = 0;
    int clauses_read = 0;
    int lit;

    while (fscanf(fp, "%d", &lit) == 1) {
        if (lit == 0) {
            /* End of clause */
#ifdef USE_HW_BCP
            if (lit_count > 5)
                fprintf(stderr, "Warning: clause %d has %d literals (hardware max is 5)\n",
                        clauses_read, lit_count);
#endif
            cdcl_add_clause(s, lits, lit_count);
            lit_count = 0;
            clauses_read++;
        } else {
            if (lit_count >= num_vars) {
                /* Grow buffer if needed */
                lits = (int *)realloc(lits, (lit_count + 1) * sizeof(int));
            }
            lits[lit_count++] = lit;
        }
    }

    /* Handle trailing clause without final 0 */
    if (lit_count > 0) {
        cdcl_add_clause(s, lits, lit_count);
        clauses_read++;
    }

    free(lits);
    fclose(fp);

    if (clauses_read != num_clauses) {
        fprintf(stderr, "Warning: header declared %d clauses, read %d\n",
                num_clauses, clauses_read);
    }

    /* Solve */
    int result = cdcl_solve(s);

    /* Output result in DIMACS format */
    if (result == SAT) {
        printf("s SATISFIABLE\n");
        printf("v ");
        for (int v = 1; v <= num_vars; v++) {
            int val = cdcl_get_value(s, v);
            if (val == 1)
                printf("%d ", v);
            else
                printf("%d ", -v);
        }
        printf("0\n");
    } else {
        printf("s UNSATISFIABLE\n");
    }

    cdcl_destroy(s);
    return (result == SAT) ? 0 : 1;
}
