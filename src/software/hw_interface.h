/*
 * hw_interface.h — Hardware BCP Accelerator Serial Driver
 *
 * Provides functions to communicate with the BCP accelerator over UART.
 * All declarations are gated behind USE_HW_BCP so the software-only build
 * is unaffected.
 */

#ifndef HW_INTERFACE_H
#define HW_INTERFACE_H

#ifdef USE_HW_BCP

#include "CDCL.h"

/* Global serial port path — set by main() before calling cdcl_solve().
 * If NULL, hw_open() uses the default port. */
extern const char *hw_port;

/* Open the serial port to the FPGA.
 * If `port` is NULL, defaults to "/dev/cu.usbserial-000000".
 * Configures 1 Mbaud, 8N1, raw mode, no flow control.
 * Returns 0 on success, -1 on error. */
int  hw_open(const char *port);

/* Close the serial port. */
void hw_close(void);

/* Upload the entire problem state (clauses, watch lists, assignments)
 * to the FPGA so the hardware memories match the solver's state. */
void hw_init(CDCLSolver *s);

/* Send a single WRITE_ASSIGN command to update one variable on the FPGA.
 * `val` uses the software encoding: 0=FALSE, 1=TRUE, -1=UNASSIGNED. */
void hw_write_assign(int var, int val);

/* After backtracking to `from_level`, unassign all variables on the FPGA
 * that are no longer on the trail. */
void hw_sync_assigns(CDCLSolver *s, int from_level);

/* Run BCP on the hardware accelerator.
 * Processes trail entries from s->prop_head to s->trail_size.
 * Enqueues implications into the solver and returns:
 *   -1 if no conflict, or the conflicting clause index. */
int  hw_propagate(CDCLSolver *s);

#endif /* USE_HW_BCP */
#endif /* HW_INTERFACE_H */
