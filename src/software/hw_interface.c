/*
 * hw_interface.c — Hardware BCP Accelerator Serial Driver
 *
 * Communicates with the BCP accelerator FPGA over UART using the command
 * protocol defined in host_interface.py.  Translates between the C solver's
 * data representation and the hardware's encoding.
 *
 * Protocol (Host → FPGA):
 *   0x01 WRITE_CLAUSE   [clause_id:2][size:1][sat:1][lit0..4:10]  14 bytes
 *   0x02 WRITE_WL_ENTRY [lit:2][idx:1][clause_id:2]                5 bytes
 *   0x03 WRITE_WL_LEN   [lit:2][len:1]                             3 bytes
 *   0x04 WRITE_ASSIGN   [var:2][val:1]                             3 bytes
 *   0x05 BCP_START      [false_lit:2]                              2 bytes
 *
 * Protocol (FPGA → Host):
 *   0xB0 [var:2][val:1][reason:2]  — implication  (6 bytes)
 *   0xC0 [clause_id:2][0x00]       — done, no conflict (4 bytes)
 *   0xC1 [clause_id:2][0x00]       — done, conflict    (4 bytes)
 */

#ifdef USE_HW_BCP

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <termios.h>
#include <sys/ioctl.h>
#ifdef __APPLE__
#include <IOKit/serial/ioss.h>
#endif

#include "CDCL.h"
#include "hw_interface.h"

/* ── Command bytes ──────────────────────────────────────────────────────── */
#define CMD_WRITE_CLAUSE   0x01
#define CMD_WRITE_WL_ENTRY 0x02
#define CMD_WRITE_WL_LEN   0x03
#define CMD_WRITE_ASSIGN   0x04
#define CMD_BCP_START      0x05

/* ── Response bytes ─────────────────────────────────────────────────────── */
#define RSP_IMPLICATION    0xB0
#define RSP_DONE_OK        0xC0
#define RSP_DONE_CONFLICT  0xC1

/* ── Hardware assignment encoding ───────────────────────────────────────── */
/* Software: -1=UNASSIGNED, 0=FALSE, 1=TRUE                                */
/* Hardware:  0=UNASSIGNED, 1=FALSE, 2=TRUE                                */
#define HW_UNASSIGNED 0
#define HW_FALSE      1
#define HW_TRUE       2

/* Default serial port */
#define DEFAULT_PORT "/dev/cu.usbserial-000000"

/* ── Global port path (set by main before cdcl_solve) ──────────────────── */
const char *hw_port = NULL;

/* ── Static state ───────────────────────────────────────────────────────── */
static int serial_fd = -1;

/* ── Helper: map software assign value → hardware encoding ──────────────── */
static inline unsigned char sw_to_hw_assign(int val) {
    if (val == 1)  return HW_TRUE;
    if (val == 0)  return HW_FALSE;
    return HW_UNASSIGNED;  /* UNASSIGNED = -1 */
}

/* ── Helper: map hardware assign value → software encoding ──────────────── */
static inline int hw_to_sw_assign(unsigned char val) {
    if (val == HW_TRUE)  return 1;
    if (val == HW_FALSE) return 0;
    return UNASSIGNED;
}

/* ── Serial I/O helpers ─────────────────────────────────────────────────── */

static int send_bytes(const unsigned char *buf, int len) {
    int total = 0;
    while (total < len) {
        int n = (int)write(serial_fd, buf + total, len - total);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("hw_interface: write");
            return -1;
        }
        total += n;
    }
    tcdrain(serial_fd);
    return 0;
}

static int recv_bytes(unsigned char *buf, int len) {
    int total = 0;
    while (total < len) {
        int n = (int)read(serial_fd, buf + total, len - total);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("hw_interface: read");
            return -1;
        }
        if (n == 0) {
            /* Timeout with no data — retry */
            continue;
        }
        total += n;
    }
    return 0;
}

static int send_cmd(unsigned char cmd, const unsigned char *payload, int payload_len) {
    if (send_bytes(&cmd, 1) < 0) return -1;
    if (payload_len > 0 && send_bytes(payload, payload_len) < 0) return -1;
    return 0;
}

/* ── Public API ─────────────────────────────────────────────────────────── */

int hw_open(const char *port) {
    if (port == NULL) port = DEFAULT_PORT;

    serial_fd = open(port, O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (serial_fd < 0) {
        perror("hw_interface: open");
        return -1;
    }

    /* Clear non-blocking after open */
    fcntl(serial_fd, F_SETFL, 0);

    struct termios tty;
    memset(&tty, 0, sizeof(tty));
    if (tcgetattr(serial_fd, &tty) != 0) {
        perror("hw_interface: tcgetattr");
        close(serial_fd);
        serial_fd = -1;
        return -1;
    }

    /* Raw mode — no echo, no signals, no canonical processing */
    cfmakeraw(&tty);

    /* 8N1, no flow control */
    tty.c_cflag &= ~(CSTOPB | CRTSCTS);
    tty.c_cflag |= CS8 | CLOCAL | CREAD;

    /* Set a placeholder baud rate for tcsetattr (will override below on macOS) */
    cfsetispeed(&tty, B115200);
    cfsetospeed(&tty, B115200);

    /* Non-blocking reads with 100ms timeout */
    tty.c_cc[VMIN]  = 0;
    tty.c_cc[VTIME] = 1;  /* 100ms in deciseconds */

    if (tcsetattr(serial_fd, TCSANOW, &tty) != 0) {
        perror("hw_interface: tcsetattr");
        close(serial_fd);
        serial_fd = -1;
        return -1;
    }

#ifdef __APPLE__
    /* macOS: set 1 Mbaud via IOSSIOSPEED ioctl (FTDI doesn't support
     * non-standard rates through cfsetspeed/tcsetattr) */
    speed_t speed = 1000000;
    if (ioctl(serial_fd, IOSSIOSPEED, &speed) < 0) {
        perror("hw_interface: IOSSIOSPEED");
        close(serial_fd);
        serial_fd = -1;
        return -1;
    }
#endif

    /* Flush any stale data */
    tcflush(serial_fd, TCIOFLUSH);

    return 0;
}

void hw_close(void) {
    if (serial_fd >= 0) {
        close(serial_fd);
        serial_fd = -1;
    }
}

void hw_init(CDCLSolver *s) {
    unsigned char payload[14];

    /* 1. Upload clauses */
    for (int ci = 0; ci < s->clause_count; ci++) {
        Clause *c = s->clauses[ci];
        int size = c->size;
        if (size > 5) size = 5;  /* hardware supports max 5 literals */

        /* clause_id big-endian */
        payload[0] = (ci >> 8) & 0xFF;
        payload[1] = ci & 0xFF;
        /* size */
        payload[2] = (unsigned char)size;
        /* sat bit (0 at init) */
        payload[3] = 0;
        /* literals 0..4, big-endian 2 bytes each */
        for (int k = 0; k < 5; k++) {
            int lit = (k < size) ? c->lits[k] : 0;
            payload[4 + k * 2]     = (lit >> 8) & 0xFF;
            payload[4 + k * 2 + 1] = lit & 0xFF;
        }
        send_cmd(CMD_WRITE_CLAUSE, payload, 14);
    }

    /* 2. Upload watch lists */
    int num_lits = 2 * s->num_vars + 2;
    for (int lit = 0; lit < num_lits; lit++) {
        int wlen = s->watch_size[lit];
        if (wlen == 0) continue;

        /* Send watch list length */
        payload[0] = (lit >> 8) & 0xFF;
        payload[1] = lit & 0xFF;
        payload[2] = (unsigned char)wlen;
        send_cmd(CMD_WRITE_WL_LEN, payload, 3);

        /* Send each watch entry */
        for (int j = 0; j < wlen; j++) {
            int clause_id = s->watches[lit][j];
            payload[0] = (lit >> 8) & 0xFF;
            payload[1] = lit & 0xFF;
            payload[2] = (unsigned char)j;
            payload[3] = (clause_id >> 8) & 0xFF;
            payload[4] = clause_id & 0xFF;
            send_cmd(CMD_WRITE_WL_ENTRY, payload, 5);
        }
    }

    /* 3. Upload variable assignments */
    for (int var = 1; var <= s->num_vars; var++) {
        payload[0] = (var >> 8) & 0xFF;
        payload[1] = var & 0xFF;
        payload[2] = sw_to_hw_assign(s->assigns[var]);
        send_cmd(CMD_WRITE_ASSIGN, payload, 3);
    }
}

void hw_write_assign(int var, int val) {
    unsigned char payload[3];
    payload[0] = (var >> 8) & 0xFF;
    payload[1] = var & 0xFF;
    payload[2] = sw_to_hw_assign(val);
    send_cmd(CMD_WRITE_ASSIGN, payload, 3);
}

void hw_sync_assigns(CDCLSolver *s, int from_level) {
    /* After backtracking, any variable no longer on the trail should be
     * marked UNASSIGNED on the FPGA.  We just re-send the current state
     * for all variables — the solver has already unassigned them. */
    for (int var = 1; var <= s->num_vars; var++) {
        if (s->assigns[var] == UNASSIGNED) {
            hw_write_assign(var, UNASSIGNED);
        }
    }
}

int hw_propagate(CDCLSolver *s) {
    unsigned char payload[2];
    unsigned char resp[6];

    while (s->prop_head < s->trail_size) {
        /* The literal that just became true — watch list of its negation */
        int true_lit = s->trail[s->prop_head];
        int false_lit = true_lit ^ 1;

        /* Send BCP_START with false_lit (big-endian) */
        payload[0] = (false_lit >> 8) & 0xFF;
        payload[1] = false_lit & 0xFF;
        send_cmd(CMD_BCP_START, payload, 2);

        /* Read response packets */
        int conflict_ci = -1;
        int done = 0;

        while (!done) {
            /* Read response type byte */
            if (recv_bytes(resp, 1) < 0) return -1;

            switch (resp[0]) {
            case RSP_IMPLICATION: {
                /* Read 5 more bytes: var(2) + val(1) + reason(2) */
                if (recv_bytes(resp + 1, 5) < 0) return -1;

                int var    = (resp[1] << 8) | resp[2];
                int hw_val = resp[3];
                int reason = (resp[4] << 8) | resp[5];

                /* Convert hardware value to literal code:
                 * HW_TRUE (2) → positive lit = 2*var (even)
                 * HW_FALSE (1) → negative lit = 2*var+1 (odd) */
                int code;
                if (hw_val == HW_TRUE)
                    code = 2 * var;       /* assign TRUE → even code */
                else
                    code = 2 * var + 1;   /* assign FALSE → odd code */

                /* Enqueue into the solver */
                s->assigns[var] = (code & 1) ? 0 : 1;
                s->levels[var]  = s->num_decisions;
                s->reasons[var] = reason;
                s->trail[s->trail_size++] = code;

                /* Also sync this new assignment to the FPGA so subsequent
                 * BCP rounds see it */
                hw_write_assign(var, s->assigns[var]);
                break;
            }
            case RSP_DONE_OK:
                /* Read 3 more bytes: clause_id(2) + padding(1) */
                if (recv_bytes(resp + 1, 3) < 0) return -1;
                done = 1;
                break;

            case RSP_DONE_CONFLICT:
                /* Read 3 more bytes: clause_id(2) + padding(1) */
                if (recv_bytes(resp + 1, 3) < 0) return -1;
                conflict_ci = (resp[1] << 8) | resp[2];
                done = 1;
                break;

            default:
                fprintf(stderr, "hw_interface: unexpected response byte 0x%02X\n",
                        resp[0]);
                return -1;
            }
        }

        if (conflict_ci >= 0) {
            /* Advance prop_head past the literal we just processed */
            s->prop_head++;
            return conflict_ci;
        }

        /* Advance to next trail entry (new implications may have extended it) */
        s->prop_head++;
    }

    return -1;  /* no conflict */
}

#endif /* USE_HW_BCP */
