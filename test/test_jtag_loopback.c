/*
 * test_jtag_loopback.c — Standalone JTAG communication test
 *
 * Validates the JTAG communication path between host and FPGA:
 *   1. Starts OpenOCD with the project's config
 *   2. Connects to the TCL server
 *   3. Sends a WRITE_ASSIGN command and reads back status
 *   4. Sends BCP_START on an empty watch list (should get DONE_OK)
 *
 * Build:  gcc -O2 -Wall -o test_jtag_loopback test/test_jtag_loopback.c
 * Usage:  ./test_jtag_loopback [openocd-cfg-path]
 *
 * Prerequisites:
 *   - FPGA flashed with bcp_accel_jtag bitstream
 *   - openocd in PATH (or oss-cad-suite activated)
 *   - No other OpenOCD instance running
 */

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

/* ── Constants ─────────────────────────────────────────────────────────── */
#define TCL_PORT     6666
#define TCL_TERM     '\x1a'

#define CMD_WRITE_ASSIGN 0x04
#define CMD_BCP_START    0x05
#define CMD_ACK_IMPL     0x07

#define RSP_IDLE         0x00
#define RSP_BUSY         0x01
#define RSP_IMPLICATION  0xB0
#define RSP_DONE_OK      0xC0
#define RSP_DONE_CONF    0xC1

/* Default OpenOCD config */
#define DEFAULT_CFG "openocd-ecp5.cfg"

/* ── Globals ───────────────────────────────────────────────────────────── */
static int tcl_sock = -1;
static pid_t ocd_pid = -1;
static unsigned char seq = 0;

/* ── Cleanup ───────────────────────────────────────────────────────────── */
static void cleanup(void) {
    if (tcl_sock >= 0) {
        char cmd[] = "shutdown";
        send(tcl_sock, cmd, strlen(cmd), 0);
        char t = TCL_TERM;
        send(tcl_sock, &t, 1, 0);
        close(tcl_sock);
        tcl_sock = -1;
    }
    if (ocd_pid > 0) {
        usleep(200000);
        int st;
        if (waitpid(ocd_pid, &st, WNOHANG) == 0) {
            kill(ocd_pid, SIGTERM);
            waitpid(ocd_pid, &st, 0);
        }
        ocd_pid = -1;
    }
}

static void sighandler(int sig) {
    (void)sig;
    cleanup();
    _exit(1);
}

/* ── TCL helpers ───────────────────────────────────────────────────────── */
static int tcl_send(const char *cmd) {
    int len = (int)strlen(cmd);
    if (send(tcl_sock, cmd, len, 0) != len) return -1;
    char t = TCL_TERM;
    if (send(tcl_sock, &t, 1, 0) != 1) return -1;
    return 0;
}

static int tcl_recv(char *buf, int bufsize) {
    int total = 0;
    while (total < bufsize - 1) {
        int n = (int)recv(tcl_sock, buf + total, 1, 0);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) continue;
            return -1;
        }
        if (buf[total] == TCL_TERM) {
            buf[total] = '\0';
            return total;
        }
        total += n;
    }
    buf[total] = '\0';
    return total;
}

static int tcl_cmd(const char *cmd, char *resp, int resp_size) {
    if (tcl_send(cmd) < 0) {
        fprintf(stderr, "  ERROR: failed to send TCL command\n");
        return -1;
    }
    if (tcl_recv(resp, resp_size) < 0) {
        fprintf(stderr, "  ERROR: failed to receive TCL response\n");
        return -1;
    }
    return 0;
}

/* Forward declaration */
static void decode_diagnostic(const char *hex);

/* ── JTAG drscan ───────────────────────────────────────────────────────── */

static void build_hex(char *out, unsigned char cmd_byte,
                      const unsigned char *payload, int plen) {
    unsigned char reg[16];
    memset(reg, 0, sizeof(reg));
    reg[0] = cmd_byte;
    for (int i = 0; i < plen && i < 14; i++)
        reg[1 + i] = payload[i];
    seq++;
    reg[15] = seq;
    for (int i = 0; i < 16; i++)
        sprintf(out + i * 2, "%02x", reg[i]);
    out[32] = '\0';
}

typedef struct {
    unsigned char status;
    unsigned int var;
    unsigned char val;
    unsigned int reason_id;
    unsigned char ack_seq;
    char raw[64];
} Response;

static int parse_response(const char *hex, Response *r) {
    /* OpenOCD may prefix with a status byte (0x00 for OK).
     * Find the hex data. */
    const char *p = hex;
    /* Skip leading non-hex characters */
    while (*p && !( (*p >= '0' && *p <= '9') ||
                    (*p >= 'a' && *p <= 'f') ||
                    (*p >= 'A' && *p <= 'F') )) p++;

    unsigned char bytes[16];
    memset(bytes, 0, sizeof(bytes));
    int count = 0;
    while (count < 16 && p[0] && p[1]) {
        unsigned int v;
        if (sscanf(p, "%2x", &v) != 1) break;
        bytes[count++] = (unsigned char)v;
        p += 2;
        while (*p == ' ') p++;
    }

    if (r) {
        r->status    = bytes[0];
        r->var       = (bytes[1] << 8) | bytes[2];
        r->val       = bytes[3];
        r->reason_id = (bytes[4] << 8) | bytes[5];
        r->ack_seq   = bytes[15];
        strncpy(r->raw, hex, sizeof(r->raw) - 1);
        r->raw[sizeof(r->raw) - 1] = '\0';
    }
    return count;
}

static int drscan(unsigned char cmd_byte,
                  const unsigned char *payload, int plen,
                  Response *rsp) {
    char hex[33];
    build_hex(hex, cmd_byte, payload, plen);

    char cmd[256];
    snprintf(cmd, sizeof(cmd),
             "irscan ecp5.tap 0x32; drscan ecp5.tap 128 0x%s", hex);

    char resp[256];
    if (tcl_cmd(cmd, resp, sizeof(resp)) < 0) return -1;

    printf("    [DEBUG] TCL response: '");
    for (int i = 0; resp[i]; i++) {
        if (resp[i] >= 0x20 && resp[i] < 0x7f)
            putchar(resp[i]);
        else
            printf("\\x%02x", (unsigned char)resp[i]);
    }
    printf("'\n");

    if (rsp) {
        int n = parse_response(resp, rsp);
        if (n < 16) {
            fprintf(stderr, "    WARNING: only parsed %d/16 response bytes "
                    "from: '%s'\n", n, resp);
        }
    }
    decode_diagnostic(resp);
    return 0;
}

static int nop_scan(Response *rsp) {
    return drscan(0x00, NULL, 0, rsp);
}

static int read_response(Response *rsp) {
    if (nop_scan(NULL) < 0) return -1;
    return nop_scan(rsp);
}

/* ── Diagnostic response decoding ──────────────────────────────────── */
/*
 * When diagnostic_mode=True, jupdate_r loads these fields into shift_reg:
 *
 *   [7:0]     cmd_latched[0:8]     seq echo          → byte[15]
 *   [15:8]    0xA5                  marker            → byte[14]
 *   [18:16]   cmd_fifo.w_level     3 bits            → byte[13] bits 2:0
 *   [19]      cmd_fifo.w_rdy                         → byte[13] bit 3
 *   [20]      cmd_fifo.w_en                          → byte[13] bit 4
 *   [21]      cmd_latch_valid                        → byte[13] bit 5
 *   [22]      cmd_valid_jtck                         → byte[13] bit 6
 *   [23]      jupdate_r                              → byte[13] bit 7
 *   [31:24]   0x00                  padding           → byte[12]
 *   [119:32]  zeros
 *   [127:120] cmd_latched[120:128]  cmd_byte echo    → byte[0]
 */
static void decode_diagnostic(const char *hex) {
    const char *p = hex;
    while (*p && !((*p >= '0' && *p <= '9') ||
                   (*p >= 'a' && *p <= 'f') ||
                   (*p >= 'A' && *p <= 'F'))) p++;

    unsigned char bytes[16];
    memset(bytes, 0, sizeof(bytes));
    int count = 0;
    while (count < 16 && p[0] && p[1]) {
        unsigned int v;
        if (sscanf(p, "%2x", &v) != 1) break;
        bytes[count++] = (unsigned char)v;
        p += 2;
        while (*p == ' ') p++;
    }

    unsigned char cmd_echo     = bytes[0];    /* [127:120] */
    unsigned char seq_echo     = bytes[15];   /* [7:0]     */
    unsigned char marker       = bytes[14];   /* [15:8]    */
    unsigned char w_level      = bytes[13] & 0x07;        /* [18:16] */
    unsigned char w_rdy        = (bytes[13] >> 3) & 1;    /* [19]    */
    unsigned char w_en         = (bytes[13] >> 4) & 1;    /* [20]    */
    unsigned char latch_valid  = (bytes[13] >> 5) & 1;    /* [21]    */
    unsigned char cmd_valid    = (bytes[13] >> 6) & 1;    /* [22]    */
    unsigned char jup_r        = (bytes[13] >> 7) & 1;    /* [23]    */

    printf("    ┌─ DIAGNOSTIC DECODE ─────────────────────────┐\n");
    printf("    │ marker           = 0x%02X  %s│\n", marker,
           marker == 0xA5 ? "(OK: jupdate_r loaded) " : "(BAD: not 0xA5!)       ");
    printf("    │ cmd_byte_echo    = 0x%02X                     │\n", cmd_echo);
    printf("    │ seq_echo         = %-3d                      │\n", seq_echo);
    printf("    │ w_level          = %d   %s│\n", w_level,
           w_level > 0 ? "*** FIFO HAS DATA ***  " : "(empty at snapshot)    ");
    printf("    │ w_rdy            = %d                        │\n", w_rdy);
    printf("    │ w_en             = %d                        │\n", w_en);
    printf("    │ cmd_latch_valid  = %d                        │\n", latch_valid);
    printf("    │ cmd_valid_jtck   = %d                        │\n", cmd_valid);
    printf("    │ jupdate_r        = %d                        │\n", jup_r);
    printf("    └─────────────────────────────────────────────┘\n");

    if (marker != 0xA5) {
        printf("    >>> MARKER MISMATCH: diagnostic NOT loaded by jupdate_r!\n");
        printf("        This means shift_reg contains stale shift data,\n");
        printf("        not the diagnostic snapshot.\n");
    } else if (w_level > 0) {
        printf("    >>> w_level > 0: jtck clock IS reaching AsyncFIFO!\n");
    }
}

static const char *status_name(unsigned char s) {
    switch (s) {
    case RSP_IDLE:        return "IDLE";
    case RSP_BUSY:        return "BUSY";
    case RSP_IMPLICATION: return "IMPLICATION";
    case RSP_DONE_OK:     return "DONE_OK";
    case RSP_DONE_CONF:   return "DONE_CONFLICT";
    default:              return "UNKNOWN";
    }
}

/* ── Main ──────────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    const char *cfg = DEFAULT_CFG;
    if (argc > 1) cfg = argv[1];

    signal(SIGINT, sighandler);
    signal(SIGTERM, sighandler);
    atexit(cleanup);

    printf("JTAG Communication Test\n");
    printf("=======================\n\n");

    /* ── Step 1: Start OpenOCD ─────────────────────────────────────── */
    printf("[1] Starting OpenOCD with config: %s\n", cfg);

    ocd_pid = fork();
    if (ocd_pid < 0) { perror("fork"); return 1; }
    if (ocd_pid == 0) {
        /* Redirect output so we can see errors */
        freopen("/tmp/openocd_test.log", "w", stderr);
        freopen("/dev/null", "w", stdout);
        execlp("openocd", "openocd", "-f", cfg, "-c", "init", NULL);
        perror("exec openocd");
        _exit(1);
    }

    printf("    OpenOCD PID: %d\n", ocd_pid);
    printf("    Waiting for startup...\n");
    usleep(1000000);  /* 1 second */

    /* Check if OpenOCD is still running */
    int st;
    if (waitpid(ocd_pid, &st, WNOHANG) != 0) {
        fprintf(stderr, "    ERROR: OpenOCD exited immediately.\n");
        fprintf(stderr, "    Check /tmp/openocd_test.log for details.\n");
        ocd_pid = -1;
        return 1;
    }
    printf("    OpenOCD is running.\n\n");

    /* ── Step 2: Connect to TCL server ────────────────────────────── */
    printf("[2] Connecting to TCL server on port %d...\n", TCL_PORT);

    tcl_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (tcl_sock < 0) { perror("socket"); return 1; }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(TCL_PORT);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

    int connected = 0;
    for (int i = 0; i < 5; i++) {
        if (connect(tcl_sock, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
            connected = 1;
            break;
        }
        printf("    Retry %d/5...\n", i + 1);
        usleep(500000);
    }
    if (!connected) {
        fprintf(stderr, "    ERROR: cannot connect to OpenOCD TCL server.\n");
        return 1;
    }
    printf("    Connected.\n\n");

    /* ── Step 3: Basic connectivity test ──────────────────────────── */
    printf("[3] Testing basic JTAG connectivity...\n");
    {
        char resp[256];
        if (tcl_cmd("scan_chain", resp, sizeof(resp)) < 0) {
            fprintf(stderr, "    ERROR: scan_chain failed\n");
            return 1;
        }
        printf("    scan_chain response:\n    %s\n\n", resp);
    }

    /* ── Step 4: Read initial status (should be IDLE) ─────────────── */
    printf("[4] Reading initial JTAG status...\n");
    {
        Response rsp;
        if (read_response(&rsp) < 0) {
            fprintf(stderr, "    ERROR: failed to read status\n");
            return 1;
        }
        printf("    Status: 0x%02X (%s)\n", rsp.status, status_name(rsp.status));
        printf("    Ack seq: %d\n", rsp.ack_seq);
        printf("    Raw: %s\n", rsp.raw);
        if (rsp.status == RSP_IDLE) {
            printf("    PASS: FPGA reports IDLE\n\n");
        } else {
            printf("    WARN: expected IDLE (0x00), got 0x%02X\n\n",
                   rsp.status);
        }
    }

    /* ── Step 5: Send WRITE_ASSIGN (var=1, val=2=TRUE) ───────────── */
    printf("[5] Sending WRITE_ASSIGN (var=1, val=TRUE)...\n");
    {
        unsigned char payload[] = {0x00, 0x01, 0x02};
        Response rsp;
        if (drscan(CMD_WRITE_ASSIGN, payload, 3, &rsp) < 0) {
            fprintf(stderr, "    ERROR: drscan failed\n");
            return 1;
        }
        printf("    Sent.  Response from previous scan:\n");
        printf("    Status: 0x%02X (%s), ack_seq=%d\n",
               rsp.status, status_name(rsp.status), rsp.ack_seq);

        /* Wait for command to process */
        usleep(10000);

        /* Read back status */
        if (read_response(&rsp) < 0) {
            fprintf(stderr, "    ERROR: failed to read status\n");
            return 1;
        }
        printf("    After processing — Status: 0x%02X (%s), ack_seq=%d\n",
               rsp.status, status_name(rsp.status), rsp.ack_seq);
        if (rsp.status == RSP_IDLE && rsp.ack_seq == seq) {
            printf("    PASS: command acknowledged (ack_seq matches)\n\n");
        } else if (rsp.status == RSP_IDLE) {
            printf("    PARTIAL: IDLE but ack_seq=%d (expected %d)\n\n",
                   rsp.ack_seq, seq);
        } else {
            printf("    WARN: unexpected status 0x%02X\n\n", rsp.status);
        }
    }

    /* ── Step 6: BCP_START on empty watch list (false_lit=3) ──────── */
    printf("[6] Sending BCP_START (false_lit=3, empty watch list)...\n");
    printf("    Expecting immediate DONE_OK (no clauses watching lit 3).\n");
    {
        unsigned char payload[] = {0x00, 0x03};
        if (drscan(CMD_BCP_START, payload, 2, NULL) < 0) {
            fprintf(stderr, "    ERROR: drscan failed\n");
            return 1;
        }

        /* Poll for result */
        usleep(10000);
        Response rsp;
        int found = 0;
        for (int i = 0; i < 20; i++) {
            if (read_response(&rsp) < 0) {
                fprintf(stderr, "    ERROR: poll failed\n");
                return 1;
            }
            printf("    Poll %d: status=0x%02X (%s)\n",
                   i, rsp.status, status_name(rsp.status));
            if (rsp.status == RSP_DONE_OK || rsp.status == RSP_DONE_CONF) {
                found = 1;
                break;
            }
            if (rsp.status != RSP_BUSY && rsp.status != RSP_IDLE) {
                found = 1;
                break;
            }
            usleep(1000);
        }

        if (found && rsp.status == RSP_DONE_OK) {
            printf("    PASS: BCP completed with no conflict\n\n");
        } else if (found) {
            printf("    Status: 0x%02X (%s)\n", rsp.status,
                   status_name(rsp.status));
            printf("    WARN: unexpected result\n\n");
        } else {
            printf("    FAIL: timed out waiting for BCP result\n\n");
        }
    }

    /* ── Summary ──────────────────────────────────────────────────── */
    printf("=======================\n");
    printf("Test complete. Check results above.\n");
    printf("OpenOCD log at /tmp/openocd_test.log\n");

    return 0;
}
