/*
 * hw_interface_jtag.c — Hardware BCP Accelerator JTAG Driver
 *
 * Communicates with the BCP accelerator FPGA via JTAG using the ECP5
 * JTAGG primitive and OpenOCD's TCL server.  Provides the same API
 * as hw_interface.c so the solver code is unchanged.
 *
 * Protocol: 128-bit drscan commands via OpenOCD TCL socket.
 *
 * Command (host → FPGA):
 *   [127:120] cmd_byte    (0x01-0x07)
 *   [119:8]   payload     (14 bytes, same encoding as UART protocol)
 *   [7:0]     seq_num     (incremented per command)
 *
 * Response (FPGA → host, shifted out during drscan):
 *   [127:120] status      (0x00=IDLE, 0x01=BUSY, 0xB0=IMPL, 0xC0/C1=DONE)
 *   [119:104] var         (16 bits)
 *   [103:96]  val         (8 bits)
 *   [95:80]   reason/clause_id (16 bits)
 *   [79:8]    reserved
 *   [7:0]     ack_seq
 */

#ifdef USE_HW_BCP

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <arpa/inet.h>

#include "CDCL.h"
#include "hw_interface.h"

/* ── Command bytes ──────────────────────────────────────────────────────── */
#define CMD_WRITE_CLAUSE   0x01
#define CMD_WRITE_WL_ENTRY 0x02
#define CMD_WRITE_WL_LEN   0x03
#define CMD_WRITE_ASSIGN   0x04
#define CMD_BCP_START      0x05
#define CMD_RESET_STATE    0x06
#define CMD_ACK_IMPL       0x07

/* ── Response status bytes ─────────────────────────────────────────────── */
#define RSP_IDLE           0x00
#define RSP_BUSY           0x01
#define RSP_IMPLICATION    0xB0
#define RSP_DONE_OK        0xC0
#define RSP_DONE_CONFLICT  0xC1

/* ── Hardware assignment encoding ──────────────────────────────────────── */
#define HW_UNASSIGNED 0
#define HW_FALSE      1
#define HW_TRUE       2

/* ── OpenOCD configuration ─────────────────────────────────────────────── */
#define OPENOCD_TCL_PORT 6666
#define OPENOCD_HOST     "127.0.0.1"
#define TCL_TERMINATOR   '\x1a'  /* OpenOCD TCL protocol terminator */

/* ── Global port path (unused for JTAG, kept for API compat) ──────────── */
const char *hw_port = NULL;

/* ── Static state ───────────────────────────────────────────────────────── */
static int tcl_sock = -1;
static pid_t openocd_pid = -1;
static unsigned char seq_num = 0;

/* ── Helper: map software assign value → hardware encoding ──────────────── */
static inline unsigned char sw_to_hw_assign(int val) {
    if (val == 1)  return HW_TRUE;
    if (val == 0)  return HW_FALSE;
    return HW_UNASSIGNED;
}

/* ── Helper: map hardware assign value → software encoding ──────────────── */
static inline int hw_to_sw_assign(unsigned char val) {
    if (val == HW_TRUE)  return 1;
    if (val == HW_FALSE) return 0;
    return UNASSIGNED;
}

/* ── TCL socket I/O helpers ─────────────────────────────────────────────── */

static int tcl_send(const char *cmd) {
    /* OpenOCD TCL protocol: send command + \x1a terminator */
    int len = (int)strlen(cmd);
    if (send(tcl_sock, cmd, len, 0) != len) {
        perror("hw_interface_jtag: tcl send cmd");
        return -1;
    }
    char term = TCL_TERMINATOR;
    if (send(tcl_sock, &term, 1, 0) != 1) {
        perror("hw_interface_jtag: tcl send terminator");
        return -1;
    }
    return 0;
}

static int tcl_recv(char *buf, int bufsize) {
    /* Read until \x1a terminator */
    int total = 0;
    while (total < bufsize - 1) {
        int n = (int)recv(tcl_sock, buf + total, 1, 0);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            perror("hw_interface_jtag: tcl recv");
            return -1;
        }
        if (buf[total] == TCL_TERMINATOR) {
            buf[total] = '\0';
            return total;
        }
        total += n;
    }
    buf[total] = '\0';
    return total;
}

/* ── Build 128-bit command as hex string for drscan ──────────────────── */

static const char *cmd_name(unsigned char cmd) {
    switch (cmd) {
    case CMD_WRITE_CLAUSE:   return "WRITE_CLAUSE";
    case CMD_WRITE_WL_ENTRY: return "WRITE_WL_ENTRY";
    case CMD_WRITE_WL_LEN:   return "WRITE_WL_LEN";
    case CMD_WRITE_ASSIGN:   return "WRITE_ASSIGN";
    case CMD_BCP_START:      return "BCP_START";
    case CMD_RESET_STATE:    return "RESET_STATE";
    case CMD_ACK_IMPL:       return "ACK_IMPL";
    case 0x00:               return "NOP";
    default:                 return "UNKNOWN";
    }
}

static const char *rsp_name(unsigned char status) {
    switch (status) {
    case RSP_IDLE:          return "IDLE";
    case RSP_BUSY:          return "BUSY";
    case RSP_IMPLICATION:   return "IMPLICATION";
    case RSP_DONE_OK:       return "DONE_OK";
    case RSP_DONE_CONFLICT: return "DONE_CONFLICT";
    default:                return "UNKNOWN";
    }
}

static void build_cmd_hex(char *hex_out, unsigned char cmd_byte,
                          const unsigned char *payload, int payload_len) {
    /* 128-bit register: [127:120]=cmd, [119:8]=payload, [7:0]=seq */
    unsigned char reg[16];
    memset(reg, 0, sizeof(reg));

    /* reg[0] = bits [127:120] = cmd_byte (MSB of 128-bit value) */
    reg[0] = cmd_byte;

    /* reg[1..14] = bits [119:8] = payload bytes */
    for (int i = 0; i < payload_len && i < 14; i++) {
        reg[1 + i] = payload[i];
    }

    /* reg[15] = bits [7:0] = seq_num */
    seq_num++;
    reg[15] = seq_num;

    /* Convert to hex string (MSB first) */
    for (int i = 0; i < 16; i++) {
        sprintf(hex_out + i * 2, "%02x", reg[i]);
    }
    hex_out[32] = '\0';

    /* Debug: print command being sent */
    fprintf(stderr, "[JTAG TX] cmd=0x%02X (%s) seq=%u payload(%d)=[",
            cmd_byte, cmd_name(cmd_byte), seq_num, payload_len);
    for (int i = 0; i < payload_len; i++) {
        fprintf(stderr, "%s0x%02X", i ? " " : "", payload[i]);
    }
    fprintf(stderr, "] hex=%s\n", hex_out);
}

/* ── Perform a 128-bit drscan and parse response ────────────────────── */

typedef struct {
    unsigned char status;
    unsigned int  var;
    unsigned char val;
    unsigned int  reason_id;
    unsigned char ack_seq;
} JTAGResponse;

static int jtag_drscan(unsigned char cmd_byte,
                       const unsigned char *payload, int payload_len,
                       JTAGResponse *rsp) {
    char hex_cmd[33];
    build_cmd_hex(hex_cmd, cmd_byte, payload, payload_len);

    /* Build OpenOCD TCL command:
     * "drscan ecp5.tap 128 0x<hex>" selects ER1 via IR=0x32 and scans
     * Actually, we need to first select the ER1 register, then scan:
     *   irscan ecp5.tap 0x32
     *   drscan ecp5.tap 128 0x<hex>
     * But for efficiency, we batch them. */
    char tcl_cmd[256];
    snprintf(tcl_cmd, sizeof(tcl_cmd),
             "irscan ecp5.tap 0x32; drscan ecp5.tap 128 0x%s", hex_cmd);

    if (tcl_send(tcl_cmd) < 0) return -1;

    char resp_buf[256];
    if (tcl_recv(resp_buf, sizeof(resp_buf)) < 0) return -1;

    /* Parse hex response (OpenOCD returns hex string).
     * Skip any leading whitespace or status prefix. */
    char *hex_start = resp_buf;
    /* OpenOCD TCL responses may have a leading \x00 status byte */
    if (hex_start[0] == '\0' || hex_start[0] == ' ') hex_start++;

    /* Parse 32 hex chars into 16 bytes */
    unsigned char rsp_bytes[16];
    memset(rsp_bytes, 0, sizeof(rsp_bytes));

    /* Find the hex data in the response */
    char *p = hex_start;
    while (*p && (*p == ' ' || *p == '\n' || *p == '\r')) p++;

    for (int i = 0; i < 16 && p[0] && p[1]; i++) {
        unsigned int byte_val;
        if (sscanf(p, "%2x", &byte_val) != 1) break;
        rsp_bytes[i] = (unsigned char)byte_val;
        p += 2;
        while (*p == ' ') p++;  /* skip spaces between hex bytes */
    }

    /* Debug: print raw response */
    fprintf(stderr, "[JTAG RX] raw_hex=");
    for (int i = 0; i < 16; i++) fprintf(stderr, "%02x", rsp_bytes[i]);
    fprintf(stderr, " raw_tcl=\"%s\"\n", hex_start);

    if (rsp) {
        rsp->status    = rsp_bytes[0];
        rsp->var       = (rsp_bytes[1] << 8) | rsp_bytes[2];
        rsp->val       = rsp_bytes[3];
        rsp->reason_id = (rsp_bytes[4] << 8) | rsp_bytes[5];
        rsp->ack_seq   = rsp_bytes[15];

        fprintf(stderr, "[JTAG RX] status=0x%02X (%s) var=%u val=%u "
                "reason_id=%u ack_seq=%u\n",
                rsp->status, rsp_name(rsp->status),
                rsp->var, rsp->val, rsp->reason_id, rsp->ack_seq);
    }

    return 0;
}

/* ── NOP scan (read-only, doesn't trigger FSM) ─────────────────────── */

static int jtag_nop_scan(JTAGResponse *rsp) {
    return jtag_drscan(0x00, NULL, 0, rsp);
}

/* ── Read current response (flush + read) ──────────────────────────── */

static int jtag_read_response(JTAGResponse *rsp) {
    /* Flush scan: loads current rsp_reg into shift_reg */
    if (jtag_nop_scan(NULL) < 0) return -1;
    /* Read scan: shifts out the loaded response */
    return jtag_nop_scan(rsp);
}

/* ── Send command and wait for completion ──────────────────────────── */

static int jtag_send_cmd(unsigned char cmd_byte,
                         const unsigned char *payload, int payload_len) {
    JTAGResponse rsp;
    if (jtag_drscan(cmd_byte, payload, payload_len, &rsp) < 0) return -1;
    /* For write commands (not BCP_START), just return.
     * The 1-scan delay means the response we just read is stale,
     * but write commands are fire-and-forget. */
    return 0;
}

/* ── Poll until BCP is done ─────────────────────────────────────────── */

static int jtag_poll_status(JTAGResponse *rsp) {
    /* After BCP_START, poll with NOP scans until status != BUSY.
     * Due to the 1-scan delay, we do flush+read pairs. */
    int max_polls = 10000;
    for (int i = 0; i < max_polls; i++) {
        if (jtag_read_response(rsp) < 0) return -1;
        fprintf(stderr, "[JTAG POLL] iter=%d status=0x%02X (%s) var=%u "
                "val=%u reason=%u ack_seq=%u\n",
                i, rsp->status, rsp_name(rsp->status),
                rsp->var, rsp->val, rsp->reason_id, rsp->ack_seq);
        if (rsp->status != RSP_BUSY && rsp->status != RSP_IDLE) {
            return 0;
        }
        /* Small delay between polls */
        usleep(100);
    }
    fprintf(stderr, "hw_interface_jtag: poll timeout (last status=0x%02X)\n",
            rsp->status);
    return -1;
}

/* ── Public API ─────────────────────────────────────────────────────── */

int hw_open(const char *port) {
    (void)port;  /* unused for JTAG */

    /* Fork OpenOCD as background daemon with TCL server */
    openocd_pid = fork();
    if (openocd_pid < 0) {
        perror("hw_interface_jtag: fork");
        return -1;
    }

    if (openocd_pid == 0) {
        /* Child: exec OpenOCD with the project config */
        freopen("/dev/null", "w", stdout);
        freopen("/dev/null", "w", stderr);
        execlp("openocd", "openocd",
               "-f", "openocd-ecp5.cfg",
               "-c", "init",
               NULL);
        perror("hw_interface_jtag: exec openocd");
        _exit(1);
    }

    /* Parent: wait for OpenOCD to start, then connect */
    usleep(500000);  /* 500ms */

    /* Connect to OpenOCD TCL server */
    tcl_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (tcl_sock < 0) {
        perror("hw_interface_jtag: socket");
        return -1;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(OPENOCD_TCL_PORT);
    inet_pton(AF_INET, OPENOCD_HOST, &addr.sin_addr);

    /* Retry connection a few times (OpenOCD may still be starting) */
    int connected = 0;
    for (int i = 0; i < 10; i++) {
        if (connect(tcl_sock, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
            connected = 1;
            break;
        }
        usleep(500000);  /* 500ms between retries */
    }

    if (!connected) {
        fprintf(stderr, "hw_interface_jtag: failed to connect to OpenOCD "
                "TCL server on port %d\n", OPENOCD_TCL_PORT);
        close(tcl_sock);
        tcl_sock = -1;
        return -1;
    }

    seq_num = 0;
    return 0;
}

void hw_close(void) {
    if (tcl_sock >= 0) {
        /* Send shutdown command to OpenOCD */
        tcl_send("shutdown");
        close(tcl_sock);
        tcl_sock = -1;
    }

    if (openocd_pid > 0) {
        /* Give OpenOCD a moment to shut down gracefully */
        usleep(200000);
        int status;
        if (waitpid(openocd_pid, &status, WNOHANG) == 0) {
            /* Still running — send SIGTERM */
            kill(openocd_pid, SIGTERM);
            waitpid(openocd_pid, &status, 0);
        }
        openocd_pid = -1;
    }
}

void hw_init(CDCLSolver *s) {
    unsigned char payload[14];

    /* 1. Upload clauses */
    for (int ci = 0; ci < s->clause_count; ci++) {
        Clause *c = s->clauses[ci];
        int size = c->size;
        if (size > 5) size = 5;

        payload[0] = (ci >> 8) & 0xFF;
        payload[1] = ci & 0xFF;
        payload[2] = (unsigned char)size;
        payload[3] = 0;  /* sat bit = 0 at init */
        for (int k = 0; k < 5; k++) {
            int lit = (k < size) ? c->lits[k] : 0;
            payload[4 + k * 2]     = (lit >> 8) & 0xFF;
            payload[4 + k * 2 + 1] = lit & 0xFF;
        }
        jtag_send_cmd(CMD_WRITE_CLAUSE, payload, 14);
    }

    /* 2. Upload watch lists */
    int num_lits = 2 * s->num_vars + 2;
    for (int lit = 0; lit < num_lits; lit++) {
        int wlen = s->watch_size[lit];
        if (wlen == 0) continue;

        payload[0] = (lit >> 8) & 0xFF;
        payload[1] = lit & 0xFF;
        payload[2] = (unsigned char)wlen;
        jtag_send_cmd(CMD_WRITE_WL_LEN, payload, 3);

        for (int j = 0; j < wlen; j++) {
            int clause_id = s->watches[lit][j];
            payload[0] = (lit >> 8) & 0xFF;
            payload[1] = lit & 0xFF;
            payload[2] = (unsigned char)j;
            payload[3] = (clause_id >> 8) & 0xFF;
            payload[4] = clause_id & 0xFF;
            jtag_send_cmd(CMD_WRITE_WL_ENTRY, payload, 5);
        }
    }

    /* 3. Upload variable assignments */
    for (int var = 1; var <= s->num_vars; var++) {
        payload[0] = (var >> 8) & 0xFF;
        payload[1] = var & 0xFF;
        payload[2] = sw_to_hw_assign(s->assigns[var]);
        jtag_send_cmd(CMD_WRITE_ASSIGN, payload, 3);
    }
}

void hw_write_assign(int var, int val) {
    unsigned char payload[3];
    payload[0] = (var >> 8) & 0xFF;
    payload[1] = var & 0xFF;
    payload[2] = sw_to_hw_assign(val);
    jtag_send_cmd(CMD_WRITE_ASSIGN, payload, 3);
}

void hw_sync_assigns(CDCLSolver *s, int from_level) {
    (void)from_level;
    for (int var = 1; var <= s->num_vars; var++) {
        if (s->assigns[var] == UNASSIGNED) {
            hw_write_assign(var, UNASSIGNED);
        }
    }
}

int hw_propagate(CDCLSolver *s) {
    unsigned char payload[2];
    JTAGResponse rsp;

    while (s->prop_head < s->trail_size) {
        int true_lit = s->trail[s->prop_head];
        int false_lit = true_lit ^ 1;

        /* Send BCP_START */
        fprintf(stderr, "[HW_PROP] BCP_START false_lit=%d (true_lit=%d, var=%d)\n",
                false_lit, true_lit, true_lit / 2);
        payload[0] = (false_lit >> 8) & 0xFF;
        payload[1] = false_lit & 0xFF;
        jtag_drscan(CMD_BCP_START, payload, 2, NULL);

        /* Poll until not BUSY */
        if (jtag_poll_status(&rsp) < 0) return -1;

        int conflict_ci = -1;
        int done = 0;

        while (!done) {
            switch (rsp.status) {
            case RSP_IMPLICATION: {
                int var    = rsp.var;
                int hw_val = rsp.val;
                int reason = rsp.reason_id;

                fprintf(stderr, "[HW_PROP] IMPL: var=%d val=%d (hw=%d) reason=%d\n",
                        var, (hw_val == HW_TRUE) ? 1 : 0, hw_val, reason);

                int code;
                if (hw_val == HW_TRUE)
                    code = 2 * var;
                else
                    code = 2 * var + 1;

                s->assigns[var] = (code & 1) ? 0 : 1;
                s->levels[var]  = s->num_decisions;
                s->reasons[var] = reason;
                s->trail[s->trail_size++] = code;

                hw_write_assign(var, s->assigns[var]);

                /* Send ACK_IMPL and read next response */
                fprintf(stderr, "[HW_PROP] Sending ACK_IMPL\n");
                jtag_drscan(CMD_ACK_IMPL, NULL, 0, NULL);
                /* Wait a bit for FSM to process */
                usleep(100);
                if (jtag_poll_status(&rsp) < 0) return -1;
                break;
            }
            case RSP_DONE_OK:
                fprintf(stderr, "[HW_PROP] DONE_OK\n");
                done = 1;
                break;

            case RSP_DONE_CONFLICT:
                conflict_ci = rsp.reason_id;
                fprintf(stderr, "[HW_PROP] DONE_CONFLICT clause_id=%d\n",
                        conflict_ci);
                done = 1;
                break;

            default:
                fprintf(stderr, "hw_interface_jtag: unexpected status 0x%02X\n",
                        rsp.status);
                return -1;
            }
        }

        if (conflict_ci >= 0) {
            s->prop_head++;
            return conflict_ci;
        }

        s->prop_head++;
    }

    return -1;  /* no conflict */
}

#endif /* USE_HW_BCP */
