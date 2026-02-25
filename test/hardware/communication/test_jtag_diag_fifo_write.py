"""
Diagnostic test: verify jtck clock reaches AsyncFIFO internals.

Uses diagnostic_mode=True.  New layout with 0xA5 marker byte:

  [7:0]     cmd_latched[0:8]     seq echo
  [15:8]    0xA5                  marker (proves jupdate_r loaded)
  [18:16]   cmd_fifo.w_level      write-side FIFO level (3 bits)
  [19]      cmd_fifo.w_rdy
  [20]      cmd_fifo.w_en
  [21]      cmd_latch_valid
  [22]      cmd_valid_jtck
  [23]      jupdate_r              (should always be 1)
  [31:24]   0x00                   padding
  [119:32]  0                      reserved
  [127:120] cmd_latched[120:128]   cmd_byte echo
"""

import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from communication.jtag_host_interface import (
    JTAGHostInterface,
    CMD_WRITE_ASSIGN,
    REG_WIDTH,
)


def build_command(cmd_byte, payload_bytes, seq_num):
    val = (cmd_byte & 0xFF) << 120
    for i, b in enumerate(payload_bytes):
        val |= (b & 0xFF) << (112 - i * 8)
    val |= (seq_num & 0xFF)
    return val


def decode_diag(bits):
    seq_echo    = bits & 0xFF
    marker      = (bits >> 8) & 0xFF
    w_level     = (bits >> 16) & 0x7
    w_rdy       = (bits >> 19) & 1
    w_en        = (bits >> 20) & 1
    latch_valid = (bits >> 21) & 1
    cmd_valid   = (bits >> 22) & 1
    jup_r       = (bits >> 23) & 1
    cmd_echo    = (bits >> 120) & 0xFF
    return {
        "seq_echo": seq_echo,
        "marker": marker,
        "w_level": w_level,
        "w_rdy": w_rdy,
        "w_en": w_en,
        "cmd_latch_valid": latch_valid,
        "cmd_valid_jtck": cmd_valid,
        "jupdate_r": jup_r,
        "cmd_byte_echo": cmd_echo,
    }


async def jtag_scan(dut, ctx, cmd_byte, payload_bytes, seq_num):
    cmd_bits = build_command(cmd_byte, payload_bytes, seq_num)
    response = 0
    ctx.set(dut.jtag_sel, 1)
    ctx.set(dut.jtag_shift, 1)
    for i in range(REG_WIDTH):
        ctx.set(dut.jtag_tdi, (cmd_bits >> i) & 1)
        tdo_bit = ctx.get(dut.jtag_tdo)
        response |= (tdo_bit << i)
        await ctx.tick("jtck")
    ctx.set(dut.jtag_shift, 0)
    ctx.set(dut.jtag_update, 1)
    await ctx.tick("jtck")
    ctx.set(dut.jtag_update, 0)
    await ctx.tick("jtck")
    return response


async def wait_sync(ctx, n):
    for _ in range(n):
        await ctx.tick("sync")


def test_diag_fifo_write():
    dut = JTAGHostInterface(use_jtagg_primitive=False, diagnostic_mode=True)
    results = {}

    async def testbench(ctx):
        seq = 0
        fifo = dut._cmd_fifo

        ctx.set(dut.jtag_shift, 0)
        ctx.set(dut.jtag_update, 0)
        ctx.set(dut.jtag_tdi, 0)
        ctx.set(dut.jtag_sel, 0)
        ctx.set(dut.bcp_done, 0)
        ctx.set(dut.bcp_conflict, 0)
        ctx.set(dut.bcp_conflict_id, 0)
        ctx.set(dut.impl_valid, 0)
        await wait_sync(ctx, 4)

        # Send WRITE_ASSIGN
        seq += 1
        await jtag_scan(dut, ctx, CMD_WRITE_ASSIGN, [0x00, 0x05, 0x02], seq)

        # Probe w_level directly
        max_w_level = 0
        for _ in range(10):
            await ctx.tick("jtck")
            wl = ctx.get(fifo.w_level)
            if wl > max_w_level:
                max_w_level = wl
        results["probe_max_w_level"] = max_w_level

        # Scan 2: shifts out WRITE_ASSIGN diagnostic
        seq += 1
        rsp2 = await jtag_scan(dut, ctx, 0x00, [], seq)
        results["diag_cmd"] = decode_diag(rsp2)

        # Scan 3: shifts out NOP diagnostic
        seq += 1
        rsp3 = await jtag_scan(dut, ctx, 0x00, [], seq)
        results["diag_after"] = decode_diag(rsp3)

    sim = Simulator(dut)
    sim.add_clock(1e-8)
    sim.add_clock(1.3e-7, domain="jtck")
    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__),
                            "jtag_diag_fifo_write.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()

    all_pass = True

    def check(name, got, expected, op="=="):
        nonlocal all_pass
        if op == "==":
            ok = got == expected
        elif op == ">":
            ok = got > expected
        else:
            ok = got == expected
        if not ok:
            all_pass = False
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: "
              f"expected {op} {expected!r}  got={got!r}")

    print("\n=== Part A: Direct jtck-domain signal probing ===")
    check("Probe: max w_level > 0",
          results["probe_max_w_level"], 0, op=">")

    d_cmd = results["diag_cmd"]
    print(f"\n=== Part B: Diagnostic from WRITE_ASSIGN jupdate ===")
    print(f"  Raw: {d_cmd}")
    # At jupdate, cmd_latched still holds the PREVIOUS value (non-blocking).
    # So this diagnostic shows the state BEFORE WRITE_ASSIGN is latched.
    check("D_cmd: marker = 0xA5", d_cmd["marker"], 0xA5)
    check("D_cmd: cmd_byte echo = 0x00 (prev)", d_cmd["cmd_byte_echo"], 0x00)
    check("D_cmd: seq echo = 0 (prev)", d_cmd["seq_echo"], 0)
    check("D_cmd: w_rdy = 1", d_cmd["w_rdy"], 1)
    check("D_cmd: jupdate = 1", d_cmd["jupdate_r"], 1)

    d_after = results["diag_after"]
    print(f"\n=== Part C: Diagnostic from NOP jupdate ===")
    print(f"  Raw: {d_after}")
    # Now cmd_latched holds the WRITE_ASSIGN command from the previous scan.
    check("D_after: marker = 0xA5", d_after["marker"], 0xA5)
    check("D_after: cmd_byte echo = 0x04 (WRITE_ASSIGN)", d_after["cmd_byte_echo"], CMD_WRITE_ASSIGN)
    check("D_after: seq echo = 1", d_after["seq_echo"], 1)
    check("D_after: cmd_valid_jtck = 1", d_after["cmd_valid_jtck"], 1)

    if all_pass:
        print("\nAll diagnostic checks PASSED.")
    else:
        print("\nSome checks FAILED.")
        raise AssertionError("Diagnostic test failed")


if __name__ == "__main__":
    test_diag_fifo_write()
