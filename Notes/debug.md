COMPARISON OF  ~/Documents/Academics/Columbia/Barnard-PL-Lab/Sygus FPGA/fpga_sygus/sat_project_seed[⎇ main] and /Users/christianscaff/Documents/Academics/Columbia/Barnard-PL-Lab/Modular SAT Acceleration Platform (TreeSAP)/CDCL_Accelerator/src



● I have everything I need for a thorough comparison. Here are the concrete mismatches:

  ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

  Bug 1 — HW_TRUE/HW_FALSE encoding never matches (critical)

  hw_interface_jtag.c defines HW_FALSE=1, HW_TRUE=2 and checks:

   if (hw_val == HW_TRUE)   // == 2
       code = 2 * var;      // true literal
   else
       code = 2 * var + 1;  // false literal — ALWAYS taken

  jtag_host_interface.py does:

   rsp_val.eq(self.impl_value),  # impl_value is Signal() — 1-bit, so 0 or 1

  rsp_val is placed at bits [103:96]. The hardware sends 0 (false) or 1 (true) — never 2. So hw_val == HW_TRUE (==2) is never true. Every TRUE implication gets decoded as a FALSE literal, corrupting the entire assignment
  trail.

  ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

  Bug 2 — NOP scan (cmd=0x00) leaves cmd_pending=1 with stale data

  hw_interface_jtag.c's polling strategy sends cmd_byte=0x00 NOPs via jtag_nop_scan(). The hardware's valid_cmd check (lines 301-304) only accepts 0x01–0x07, so the FSM ignores it — but cmd_pending was already set to 1 by the toggle CDC mechanism. The 0x00 data sits in rx_data_latched.

  This creates a window in DONE_READY and IMPL_READY where the FSM's cmd_pending & valid_cmd guard correctly blocks (valid_cmd=False), but the stale cmd_pending=1 means any subsequent toggle from a real command doesn't
  re-assert cmd_pending unless rx_valid_sync != rx_valid_sync2 fires again. It mostly works but is fragile — a race where a real command arrives within the 2-FF synchronizer window of a NOP could cause the real command to
  be silently dropped.

  ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

  Bug 3 — jtag_poll_status stops on RSP_IDLE but hardware transitions through it

   if (rsp->status != RSP_BUSY && rsp->status != RSP_IDLE) return 0;

  After CMD_BCP_START, the shadow register reflects state one sync cycle behind. The first read back could show RSP_IDLE (pre-transition state). The loop correctly keeps polling. However, after the implications are
  exhausted and the FSM reaches DONE_READY, subsequent polls also see RSP_DONE_OK/RSP_DONE_CONF (0xC0/0xC1) — neither BUSY nor IDLE — so the poll exits. Then the outer hw_propagate loop calls prop_head++ and continues. On
  the next BCP_START iteration, the hardware FSM is still in DONE_READY (not IDLE), waiting for a new command. The new CMD_BCP_START does arrive and transitions it via CMD_EXEC, so it works — but it means the hardware FSM
  is relying on DONE_READY → CMD_EXEC as the re-entry path, which is correct per the hardware design. This is fine as-is but only works because that transition exists.

  ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

  Bug 4 — hw_sync_assigns skips assigned variables on backtrack

   void hw_sync_assigns(CDCLSolver *s, int from_level) {
       for (int var = 1; var <= s->num_vars; var++) {
           if (s->assigns[var] == UNASSIGNED) {   // ← only syncs UNASSIGNED
               hw_write_assign(var, UNASSIGNED);
           }
       }
   }

  This only writes UNASSIGNED to variables that are currently unassigned in software — which is the right set after a backtrack. But it never re-syncs variables that remain assigned, meaning if the hardware's assignment
  memory has gotten out of sync for any other reason (e.g., a conflict left a partial state), there is no recovery path. The reference design (hw_init) uploads the full assignment table upfront; hw_sync_assigns should
  mirror that.

  ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

  Summary table

  ┌───┬──────────────────┬───────────────────────────────────────────────────────────────────────────────────────────────────────┬──────────┐
  │ # │ Where            │ Issue                                                                                                 │ Severity │
  ├───┼──────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────┤
  │ 1 │ hw_propagate     │ HW_TRUE=2 never matches rsp_val (0 or 1) — all TRUE implications decoded as FALSE                     │ Critical │
  ├───┼──────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────┤
  │ 2 │ jtag_nop_scan    │ cmd=0x00 leaves stale cmd_pending=1 in hardware — fragile, can drop adjacent command in race          │ High     │
  ├───┼──────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────┤
  │ 3 │ jtag_poll_status │ Relies on DONE_READY→CMD_EXEC re-entry path, not IDLE→CMD_EXEC — works but differs from stated intent │ Medium   │
  ├───┼──────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────┼──────────┤
  │ 4 │ hw_sync_assigns  │ Only syncs vars currently UNASSIGNED; no full re-sync path after partial state corruption             │ Medium   │
  └───┴──────────────────┴───────────────────────────────────────────────────────────────────────────────────────────────────────┴──────────┘

  Bug #1 is the one that would cause wrong answers: every implied literal would be stored as its negation in the trail.
 ~/Documents/Academics/Columbia/Barnard-PL-Lab/Sygus FPGA/fpga_sygus/sat_project_seed[⎇ main]                                                                                                   claude-sonnet-4.6 (medium) (1x)


