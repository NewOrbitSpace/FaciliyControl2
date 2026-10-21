"""Pfeiffer HiPace with TC 400 electronics - RS-485 bring-up and validation (Turbos 2 and 3, VC100).

A standalone tool, independent of the facility control program, for proving the RS-485 link to
the two HiPace turbos before the HAL ever relies on it.  It speaks the "Pfeiffer Vacuum protocol
for RS-485" (ASCII telegrams, decimal checksum), the protocol every Pfeiffer drive electronics
(TC 110 / TC 400 / TM 700, DCU, TCP) uses, and contains everything a GUI or the HAL needs:

    PVBus     one serial port (one RS-485 segment), several addresses on it, comm statistics
    HiPace    one turbo on a bus: typed parameter read/write, snapshot, guarded control, safe exit
    validate  a check list that says whether the link and the pump are fit for remote control
    SimBus    a simulated TC 400 bus so the tool and the GUI can be exercised without hardware

Command line (the GUI is tools/hipace_rs485_gui.py):

    python tools/hipace_rs485.py ports                              # list serial ports
    python tools/hipace_rs485.py --port COM6 scan                   # who answers on this bus?
    python tools/hipace_rs485.py --port COM6 --address 1 dump       # every known parameter, once
    python tools/hipace_rs485.py --port COM6 --address 1 watch      # live, read-only
    python tools/hipace_rs485.py --port COM6 --address 1 validate   # the check list (read-only)
    python tools/hipace_rs485.py --port COM6 --address 1 --allow-control control --take-control --start
    python tools/hipace_rs485.py --port SIM1 ...                    # same against the simulator

PROTOCOL (Pfeiffer Vacuum protocol, as in the TC 400 operating instructions, "RS-485 interface")
    9600 baud, 8 data bits, no parity, 1 stop bit, no handshake.  Factory address 1 ([P:797]).
    Telegram   a a a | A A | p p p | l l | data ... | c c c | CR
               address, action (00 = query, 10 = write / reply), parameter, data length, data,
               checksum = sum of the ASCII codes of every character before it, modulo 256, as
               three decimal digits.
    Query      0010030902=?107<CR>        -> reply 0011030906000633037<CR>  (633 Hz)
    Reply data NO_DEF  parameter does not exist   _RANGE  value out of range
               _LOGIC  logic access violation - typically: this interface does not have control

TWO PUMPS ON ONE BUS
    Both TC 400 leave the factory at address 1.  Before putting them on one segment, connect ONE
    pump alone and write a new address to [P:797] (GUI: Parameters tab / "Change address"; CLI:
    `control --set-address 2`).  The RS-485 segment needs 120 ohm termination at both ends.
    Check the RS-485 pin assignment of the connector you use in the TC 400 manual - D+ / D- are
    labelled the other way round by many USB converters; swapped lines give "no reply".

!! SAFETY - read before using any control function !!
    * Control writes are rejected by the pump (_LOGIC) unless RS-485 is the selected control
      interface: [P:060] CtrlViaInt = 2.  Taking control writes that parameter.  While RS-485 has
      control the D-Sub remote inputs (the facility's Motor / Standby lines) no longer command
      the pump - stopping it is then up to this tool, the facility program over RS-485, or the
      pump's mains.  If [P:300] RemotePrio is TRUE the D-Sub remote priority input is active and
      overrides RS-485.  Verify both behaviours on your unit with the manual before relying on them.
    * Nothing is written unless control is explicitly enabled (--allow-control / the GUI switch).
      Stopping (pumping station off) is always allowed.
    * On exit the tool stops the pumps IT started and puts [P:060] back to the value it found,
      Ctrl-C included, unless told to keep them (--keep / the close dialog).
    * Only one program can own a COM port: close this tool before the facility program uses it.
    * Starting a turbo is your decision: backing pump running, foreline below the hot-start limit
      (the facility uses 5 mBar on the turbo gauge), turbo valve state as you intend.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import List, Tuple

# The protocol core lives in the facility package (the facility program drives the turbos with the
# same code).  Re-exported here so `import hipace_rs485 as hp` keeps working for the GUI and tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from facility_control.drivers.pfeiffer_pv import *  # noqa: E402,F401,F403
from facility_control.drivers.pfeiffer_pv import (  # noqa: E402,F401
    ACTION_QUERY, ACTION_WRITE, BAUD, CR, CTRL_INTERFACES, CTRL_RS485, DEFAULT_ADDRESS, DT, FAST_PARAMS,
    IDENT_PARAMS, PARAMS, SLOW_PARAMS, Check, CommStats, ControlNotEnabled, HiPace, Param, PVBus, PVChecksum,
    PVError, PVFormat, PVRejected, PVTimeout, SimBus, SimTC400, Snapshot, Telegram, build_telegram, checksum,
    ctrl_text, decode, encode, error_kind, parse_telegram, query_telegram, validate)


def list_ports() -> List[Tuple[str, str]]:
    ports = []
    try:
        from serial.tools import list_ports as lp
        ports = [(p.device, p.description or "") for p in lp.comports()]
    except Exception:
        pass
    return ports + [("SIM1", "simulator: shared bus, addresses 1 and 2"),
                    ("SIM2", "simulator: one pump, address 1"),
                    ("SIM4", "simulator: shared bus, addresses 1-4")]


# ---------------------------------------------------------------------------- CLI
def _fmt(pump: HiPace, s: Snapshot) -> str:
    pct = s.speed_pct(pump.nominal_hz)
    return (f"{s.status(pump.nominal_hz):10s} {s.get(309, '?'):>5} Hz "
            f"({'?' if pct is None else f'{pct:5.1f}'} %)  {s.get(310, '?')} A  {s.get(316, '?')} W  "
            f"motor {s.get(346, '?')} °C  err {s.get(303, '?')!s}  ctrl {ctrl_text(s.get(60))}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port of the RS-485 converter (COM6, /dev/ttyUSB0, SIM1, SIM2, SIM4)")
    ap.add_argument("--address", type=int, default=DEFAULT_ADDRESS, help="RS-485 address [P:797] (factory 1)")
    ap.add_argument("--timeout", type=float, default=0.3)
    ap.add_argument("--name", default="HiPace")
    ap.add_argument("--allow-control", action="store_true",
                    help="required for every write except stopping")
    ap.add_argument("--keep", action="store_true",
                    help="at exit, leave started pumps running and [P:060] as set")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list serial ports")
    sc = sub.add_parser("scan", help="find addresses that answer")
    sc.add_argument("--max", type=int, default=32)
    sub.add_parser("dump", help="read every known parameter once")
    w = sub.add_parser("watch", help="live, read-only")
    w.add_argument("--interval", type=float, default=1.0)
    w.add_argument("--seconds", type=float, default=None)
    v = sub.add_parser("validate", help="run the bring-up check list")
    v.add_argument("--pings", type=int, default=50)
    v.add_argument("--write-test", action="store_true", help="include the harmless write round trip")
    r = sub.add_parser("read", help="read one parameter")
    r.add_argument("param", type=int)
    c = sub.add_parser("control", help="command the pump (needs --allow-control, except --stop)")
    c.add_argument("--take-control", action="store_true", help="[P:060] = 2 (RS-485)")
    c.add_argument("--start", action="store_true", help="pumping station on")
    c.add_argument("--stop", action="store_true", help="pumping station off")
    c.add_argument("--standby", choices=["on", "off"])
    c.add_argument("--ack", action="store_true", help="acknowledge error")
    c.add_argument("--set-address", type=int, metavar="N", help="write [P:797] (one pump on the bus!)")
    c.add_argument("--hold", type=float, default=0.0, metavar="SECONDS", help="report for N seconds")
    c.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args(argv)

    if args.cmd == "ports":
        for dev, desc in list_ports():
            print(f"  {dev:12s} {desc}")
        return 0
    if not args.port:
        ap.error("--port is required")
    if args.cmd == "control":
        writes = args.take_control or args.start or args.standby or args.ack or args.set_address
        if writes and not args.allow_control:
            ap.error("these options write to the pump: pass --allow-control after reading the safety "
                     "note at the top of this file (--stop alone is always allowed)")

    try:
        bus = PVBus(args.port, timeout=args.timeout)
    except Exception as exc:
        print(f"cannot open {args.port}: {exc}", file=sys.stderr)
        return 1
    pump = HiPace(bus, args.address, args.name)
    pump.control_enabled = bool(args.allow_control)
    interrupted = {"flag": False}

    def on_signal(_sig, _frm):
        interrupted["flag"] = True
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, on_signal)

    try:
        if args.cmd == "scan":
            print(f"scanning {args.port} addresses 1..{args.max}")
            found = bus.scan(range(1, args.max + 1))
            for a, n in found:
                print(f"  address {a:3d}: {n}")
            if not found:
                print("  nothing answered - port, D+/D- polarity, termination, pump powered?")
            elif len(found) == 1 and found[0][0] == 1:
                print("  (factory address 1 - change one pump before sharing the bus)")
            return 0 if found else 2
        if args.cmd == "read":
            p = PARAMS.get(args.param)
            raw = pump.read_raw(args.param)
            val = decode(p.dtype, raw) if p else raw
            print(f"  [P:{args.param:03d}] {p.name if p else '(unknown)'} = {val!r} {p.unit if p else ''}  raw {raw!r}")
            return 0
        if args.cmd == "dump":
            print(f"{args.name} at address {args.address} on {args.port}\n")
            for n, p in PARAMS.items():
                if p.access == "w":
                    continue
                try:
                    raw = pump.read_raw(n)
                    print(f"  [P:{n:03d}] {p.name:12s} {raw:>16s}  = {decode(p.dtype, raw)!r} {p.unit}")
                except PVError as exc:
                    print(f"  [P:{n:03d}] {p.name:12s} -- {exc}")
            print(f"\n  {pump.stats.summary()}")
            return 0
        if args.cmd == "watch":
            pump.identify()
            print(f"{pump.ident.get(349)} fw {pump.ident.get(312)} nominal {pump.nominal_hz} Hz - Ctrl-C to stop\n")
            t_end = time.time() + args.seconds if args.seconds else None
            while t_end is None or time.time() < t_end:
                s = pump.snapshot()
                print(f"  {time.strftime('%H:%M:%S')}  {_fmt(pump, s)}")
                time.sleep(args.interval)
            return 0
        if args.cmd == "validate":
            checks = validate(pump, pings=args.pings, write_test=args.write_test, progress=print)
            print(f"\n  {pump.stats.summary()}")
            return {"PASS": 0, "WARN": 0, "FAIL": 3}[checks[-1].result]
        # control
        pump.identify()
        if args.set_address:
            pump.set_address(args.set_address)
            print(f"  address changed to {args.set_address}")
        if args.take_control:
            pump.take_rs485_control()
            print(f"  [P:060] = 2 (RS-485), was {ctrl_text(pump.original_ctrl if pump.original_ctrl is not None else 2)}")
        if args.ack:
            pump.ack_error()
            print("  error acknowledged")
        if args.standby:
            pump.set_standby(args.standby == "on")
            print(f"  standby {args.standby}")
        if args.start:
            print("  !! starting the turbo - you confirmed the backing / foreline conditions")
            pump.start()
            print("  pumping station ON")
        if args.stop:
            pump.stop()
            print("  pumping station OFF")
        if args.hold:
            t_end = time.time() + args.hold
            while time.time() < t_end:
                print(f"    {_fmt(pump, pump.snapshot())}")
                time.sleep(args.interval)
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except PVError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.cmd == "control":
            keep = args.keep and not interrupted["flag"]
            for m in pump.safe_exit(stop=not keep, restore=not keep):
                print("  " + m)
        bus.close()


if __name__ == "__main__":
    sys.exit(main())
