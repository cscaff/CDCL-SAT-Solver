"""
Full-stack integration test: UART ↔ HostInterface ↔ BCPAccelerator.

Instantiates BCPTop with platform=None (no FPGA pins) and drives the
uart_rx.rx_pin with bit-banged UART frames.  Reads uart_tx.tx_pin and
decodes serial output bytes.  Verifies that the byte-level protocol
matches what hw_interface.c expects.

This test exercises the entire hardware stack:
  UART RX → HostInterface → BCPAccelerator → HostInterface → UART TX

Scenarios (same setup as test_bcp_end_to_end.py Scenario A):
  1. Implication chain: a=T → b=T → c=T → d=T (no conflict)
  2. Conflict scenario

UART timing: divisor=12 → 12 clock cycles per bit, 120 cycles per byte
(start + 8 data + stop = 10 bits).
"""

import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from top import BCPTop


# ── UART bit-bang helpers ─────────────────────────────────────────────────

DIVISOR = 12  # clock cycles per bit (matches BCPTop default)

# Command bytes (must match host_interface.py / hw_interface.c)
CMD_WRITE_CLAUSE   = 0x01
CMD_WRITE_WL_ENTRY = 0x02
CMD_WRITE_WL_LEN   = 0x03
CMD_WRITE_ASSIGN   = 0x04
CMD_BCP_START      = 0x05

# Response bytes
RSP_IMPLICATION    = 0xB0
RSP_DONE_OK        = 0xC0
RSP_DONE_CONFLICT  = 0xC1

# HW assignment encoding
HW_UNASSIGNED = 0
HW_FALSE      = 1
HW_TRUE       = 2


async def uart_send_byte(ctx, rx_pin, byte_val, divisor=DIVISOR):
    """
    Bit-bang one 8N1 UART frame on rx_pin.
    Start bit (0), 8 data bits LSB-first, stop bit (1).
    Each bit held for `divisor` clock cycles.
    """
    # Start bit
    ctx.set(rx_pin, 0)
    for _ in range(divisor):
        await ctx.tick()

    # 8 data bits, LSB first
    for bit_idx in range(8):
        ctx.set(rx_pin, (byte_val >> bit_idx) & 1)
        for _ in range(divisor):
            await ctx.tick()

    # Stop bit
    ctx.set(rx_pin, 1)
    for _ in range(divisor):
        await ctx.tick()

    # Extra idle gap between bytes (let UART RX return to IDLE)
    for _ in range(divisor):
        await ctx.tick()


async def uart_recv_byte(ctx, tx_pin, divisor=DIVISOR):
    """
    Receive one 8N1 UART byte from tx_pin.
    Waits for the start bit (falling edge), then samples at mid-bit.
    Returns the received byte.
    """
    # Wait for start bit (tx_pin goes low)
    timeout = 50000
    while ctx.get(tx_pin) == 1:
        await ctx.tick()
        timeout -= 1
        if timeout <= 0:
            raise TimeoutError("Timed out waiting for UART start bit on TX")

    # We're at the beginning of the start bit.
    # Wait to mid-bit of start bit to confirm it's still low.
    for _ in range(divisor // 2):
        await ctx.tick()

    # Now sample each of the 8 data bits at mid-bit
    byte_val = 0
    for bit_idx in range(8):
        # Advance one full bit period to reach the middle of the next bit
        for _ in range(divisor):
            await ctx.tick()
        bit = ctx.get(tx_pin)
        byte_val |= (bit << bit_idx)

    # Advance through the stop bit
    for _ in range(divisor):
        await ctx.tick()

    return byte_val


async def send_command(ctx, rx_pin, cmd, payload):
    """Send a command byte followed by payload bytes over UART."""
    await uart_send_byte(ctx, rx_pin, cmd)
    for b in payload:
        await uart_send_byte(ctx, rx_pin, b)


async def recv_response(ctx, tx_pin):
    """
    Read one response packet from UART TX.
    Returns (response_type, data_bytes).
    """
    rsp_type = await uart_recv_byte(ctx, tx_pin)

    if rsp_type == RSP_IMPLICATION:
        # 5 more bytes: var_hi, var_lo, val, reason_hi, reason_lo
        data = []
        for _ in range(5):
            data.append(await uart_recv_byte(ctx, tx_pin))
        return rsp_type, data

    elif rsp_type in (RSP_DONE_OK, RSP_DONE_CONFLICT):
        # 3 more bytes: clause_id_hi, clause_id_lo, padding
        data = []
        for _ in range(3):
            data.append(await uart_recv_byte(ctx, tx_pin))
        return rsp_type, data

    else:
        raise ValueError(f"Unexpected response byte: 0x{rsp_type:02X}")


# ── Payload encoding helpers ─────────────────────────────────────────────

def encode_write_clause(clause_id, size, sat, lits):
    """Encode a WRITE_CLAUSE payload (14 bytes)."""
    payload = []
    payload.append((clause_id >> 8) & 0xFF)
    payload.append(clause_id & 0xFF)
    payload.append(size)
    payload.append(sat)
    for k in range(5):
        lit = lits[k] if k < len(lits) else 0
        payload.append((lit >> 8) & 0xFF)
        payload.append(lit & 0xFF)
    return payload


def encode_write_wl_len(lit, length):
    """Encode a WRITE_WL_LEN payload (3 bytes)."""
    return [(lit >> 8) & 0xFF, lit & 0xFF, length]


def encode_write_wl_entry(lit, idx, clause_id):
    """Encode a WRITE_WL_ENTRY payload (5 bytes)."""
    return [
        (lit >> 8) & 0xFF, lit & 0xFF,
        idx,
        (clause_id >> 8) & 0xFF, clause_id & 0xFF,
    ]


def encode_write_assign(var, val):
    """Encode a WRITE_ASSIGN payload (3 bytes)."""
    return [(var >> 8) & 0xFF, var & 0xFF, val]


def encode_bcp_start(false_lit):
    """Encode a BCP_START payload (2 bytes)."""
    return [(false_lit >> 8) & 0xFF, false_lit & 0xFF]


# ── Test: Implication chain (Scenario A) ─────────────────────────────────

def test_integration_implication_chain():
    """
    Full-stack test: upload 3 clauses forming a→b→c→d chain,
    start BCP with ¬a as false_lit, and verify 3 implications
    (b=T, c=T, d=T) plus done-no-conflict via UART byte stream.

    Clauses (same as test_bcp_end_to_end.py Scenario A):
      C0: (¬a ∨ b) → lits [3, 4]
      C1: (¬b ∨ c) → lits [5, 6]
      C2: (¬c ∨ d) → lits [7, 8]

    Watch lists:
      lit 3 (¬a) → [C0]
      lit 5 (¬b) → [C1]
      lit 7 (¬c) → [C2]

    Initial assignment: var 1 = TRUE (hw encoding 2)
    BCP trigger: false_lit = 3 (¬a)
    """
    dut = BCPTop()
    sim = Simulator(dut)
    sim.add_clock(1e-8)

    async def testbench(ctx):
        rx_pin = dut.uart_rx.rx_pin
        tx_pin = dut.uart_tx.tx_pin

        # Let the system settle
        ctx.set(rx_pin, 1)  # idle high
        for _ in range(50):
            await ctx.tick()

        # ── Upload clauses ────────────────────────────────────────────
        # C0: (¬a ∨ b) → lits [3, 4], size=2
        await send_command(ctx, rx_pin, CMD_WRITE_CLAUSE,
                           encode_write_clause(0, 2, 0, [3, 4]))

        # C1: (¬b ∨ c) → lits [5, 6], size=2
        await send_command(ctx, rx_pin, CMD_WRITE_CLAUSE,
                           encode_write_clause(1, 2, 0, [5, 6]))

        # C2: (¬c ∨ d) → lits [7, 8], size=2
        await send_command(ctx, rx_pin, CMD_WRITE_CLAUSE,
                           encode_write_clause(2, 2, 0, [7, 8]))

        # ── Upload watch lists ────────────────────────────────────────
        # lit 3 (¬a): length 1, entry [0] = clause 0
        await send_command(ctx, rx_pin, CMD_WRITE_WL_LEN,
                           encode_write_wl_len(3, 1))
        await send_command(ctx, rx_pin, CMD_WRITE_WL_ENTRY,
                           encode_write_wl_entry(3, 0, 0))

        # lit 5 (¬b): length 1, entry [0] = clause 1
        await send_command(ctx, rx_pin, CMD_WRITE_WL_LEN,
                           encode_write_wl_len(5, 1))
        await send_command(ctx, rx_pin, CMD_WRITE_WL_ENTRY,
                           encode_write_wl_entry(5, 0, 1))

        # lit 7 (¬c): length 1, entry [0] = clause 2
        await send_command(ctx, rx_pin, CMD_WRITE_WL_LEN,
                           encode_write_wl_len(7, 1))
        await send_command(ctx, rx_pin, CMD_WRITE_WL_ENTRY,
                           encode_write_wl_entry(7, 0, 2))

        # ── Upload assignments ────────────────────────────────────────
        # var 1 = TRUE (hw=2), vars 2-4 = UNASSIGNED (hw=0)
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(1, HW_TRUE))
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(2, HW_UNASSIGNED))
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(3, HW_UNASSIGNED))
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(4, HW_UNASSIGNED))

        # ── Start BCP with false_lit=3 (¬a) ──────────────────────────
        await send_command(ctx, rx_pin, CMD_BCP_START,
                           encode_bcp_start(3))

        # ── Read responses ────────────────────────────────────────────
        # The BCP accelerator processes one false_lit at a time (single
        # round). With the implication chain, the HW only produces
        # implications from the watch list of the initial false_lit (3).
        # That's C0 → b=TRUE, which is a single implication.
        # The host does NOT automatically chain — the C driver is
        # responsible for sending subsequent BCP_START commands for
        # newly implied literals (see hw_propagate in hw_interface.c).
        #
        # So we expect:
        #   1 implication (b=TRUE, reason=0) + done-no-conflict

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_IMPLICATION, \
            f"Expected implication (0xB0), got 0x{rsp_type:02X}"
        imp_var = (data[0] << 8) | data[1]
        imp_val = data[2]
        imp_reason = (data[3] << 8) | data[4]
        assert imp_var == 2, f"Expected var=2 (b), got {imp_var}"
        assert imp_val == 1, f"Expected val=1 (TRUE), got {imp_val}"
        assert imp_reason == 0, f"Expected reason=0 (C0), got {imp_reason}"
        print(f"  PASS: implication var={imp_var} val={imp_val} reason={imp_reason}")

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_DONE_OK, \
            f"Expected done-ok (0xC0), got 0x{rsp_type:02X}"
        print(f"  PASS: done-no-conflict")

        # ── Chain: send BCP_START for the new false_lit from b=TRUE ───
        # b=TRUE means false_lit = 5 (¬b)
        # First update assignment for var 2 = TRUE
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(2, HW_TRUE))
        await send_command(ctx, rx_pin, CMD_BCP_START,
                           encode_bcp_start(5))

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_IMPLICATION, \
            f"Expected implication (0xB0), got 0x{rsp_type:02X}"
        imp_var = (data[0] << 8) | data[1]
        imp_val = data[2]
        imp_reason = (data[3] << 8) | data[4]
        assert imp_var == 3, f"Expected var=3 (c), got {imp_var}"
        assert imp_val == 1, f"Expected val=1 (TRUE), got {imp_val}"
        assert imp_reason == 1, f"Expected reason=1 (C1), got {imp_reason}"
        print(f"  PASS: implication var={imp_var} val={imp_val} reason={imp_reason}")

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_DONE_OK, \
            f"Expected done-ok (0xC0), got 0x{rsp_type:02X}"
        print(f"  PASS: done-no-conflict")

        # ── Chain: send BCP_START for c=TRUE → false_lit = 7 (¬c) ────
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(3, HW_TRUE))
        await send_command(ctx, rx_pin, CMD_BCP_START,
                           encode_bcp_start(7))

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_IMPLICATION, \
            f"Expected implication (0xB0), got 0x{rsp_type:02X}"
        imp_var = (data[0] << 8) | data[1]
        imp_val = data[2]
        imp_reason = (data[3] << 8) | data[4]
        assert imp_var == 4, f"Expected var=4 (d), got {imp_var}"
        assert imp_val == 1, f"Expected val=1 (TRUE), got {imp_val}"
        assert imp_reason == 2, f"Expected reason=2 (C2), got {imp_reason}"
        print(f"  PASS: implication var={imp_var} val={imp_val} reason={imp_reason}")

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_DONE_OK, \
            f"Expected done-ok (0xC0), got 0x{rsp_type:02X}"
        print(f"  PASS: done-no-conflict (final)")

        print("\n  Integration test (implication chain): ALL PASSED")

    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__), "integration_impl_chain.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()


# ── Test: Conflict scenario ──────────────────────────────────────────────

def test_integration_conflict():
    """
    Full-stack test: upload clauses that lead to a conflict.

    Clauses:
      C0: (¬e ∨ f)   → lits [11, 12]
      C1: (¬f ∨ ¬g)  → lits [13, 15]

    Watch lists:
      lit 11 (¬e) → [C0]
      lit 13 (¬f) → [C1]

    Initial assignment: var 5 (e) = TRUE, var 7 (g) = TRUE
    BCP trigger: false_lit = 11 (¬e)

    Expected:
      Round 1: BCP on false_lit=11 → implication f=TRUE (var=6, val=1, reason=0) + done-ok
      Round 2: Update f=TRUE, BCP on false_lit=13 (¬f) → conflict on C1 (¬g is FALSE since g=TRUE)
    """
    dut = BCPTop()
    sim = Simulator(dut)
    sim.add_clock(1e-8)

    async def testbench(ctx):
        rx_pin = dut.uart_rx.rx_pin
        tx_pin = dut.uart_tx.tx_pin

        ctx.set(rx_pin, 1)
        for _ in range(50):
            await ctx.tick()

        # ── Upload clauses ────────────────────────────────────────────
        await send_command(ctx, rx_pin, CMD_WRITE_CLAUSE,
                           encode_write_clause(0, 2, 0, [11, 12]))
        await send_command(ctx, rx_pin, CMD_WRITE_CLAUSE,
                           encode_write_clause(1, 2, 0, [13, 15]))

        # ── Upload watch lists ────────────────────────────────────────
        await send_command(ctx, rx_pin, CMD_WRITE_WL_LEN,
                           encode_write_wl_len(11, 1))
        await send_command(ctx, rx_pin, CMD_WRITE_WL_ENTRY,
                           encode_write_wl_entry(11, 0, 0))

        await send_command(ctx, rx_pin, CMD_WRITE_WL_LEN,
                           encode_write_wl_len(13, 1))
        await send_command(ctx, rx_pin, CMD_WRITE_WL_ENTRY,
                           encode_write_wl_entry(13, 0, 1))

        # ── Upload assignments ────────────────────────────────────────
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(5, HW_TRUE))     # e = TRUE
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(6, HW_UNASSIGNED))  # f = unassigned
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(7, HW_TRUE))     # g = TRUE

        # ── Round 1: BCP on false_lit=11 (¬e) ────────────────────────
        await send_command(ctx, rx_pin, CMD_BCP_START,
                           encode_bcp_start(11))

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_IMPLICATION, \
            f"Expected implication (0xB0), got 0x{rsp_type:02X}"
        imp_var = (data[0] << 8) | data[1]
        imp_val = data[2]
        imp_reason = (data[3] << 8) | data[4]
        assert imp_var == 6, f"Expected var=6 (f), got {imp_var}"
        assert imp_val == 1, f"Expected val=1 (TRUE), got {imp_val}"
        assert imp_reason == 0, f"Expected reason=0, got {imp_reason}"
        print(f"  PASS: implication var={imp_var} val={imp_val} reason={imp_reason}")

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_DONE_OK, \
            f"Expected done-ok (0xC0), got 0x{rsp_type:02X}"
        print(f"  PASS: round 1 done-no-conflict")

        # ── Round 2: Update f=TRUE, BCP on false_lit=13 (¬f) ─────────
        await send_command(ctx, rx_pin, CMD_WRITE_ASSIGN,
                           encode_write_assign(6, HW_TRUE))
        await send_command(ctx, rx_pin, CMD_BCP_START,
                           encode_bcp_start(13))

        rsp_type, data = await recv_response(ctx, tx_pin)
        assert rsp_type == RSP_DONE_CONFLICT, \
            f"Expected done-conflict (0xC1), got 0x{rsp_type:02X}"
        conflict_id = (data[0] << 8) | data[1]
        assert conflict_id == 1, f"Expected conflict clause=1, got {conflict_id}"
        print(f"  PASS: conflict on clause {conflict_id}")

        print("\n  Integration test (conflict): ALL PASSED")

    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__), "integration_conflict.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()


if __name__ == "__main__":
    test_integration_implication_chain()
    test_integration_conflict()
