"""Read-only bring-up check for the real cDAQ – run this BEFORE the first `run.bat --daq`.

    python tools/daq_check.py [--config config/facility_main_v4.4.yaml] [--seconds 20]
    (Windows, from the project folder:  %LOCALAPPDATA%\\FacilityControl\\venv\\Scripts\\python tools\\daq_check.py)

What it does – and nothing else:
  1. lists the NI devices the driver sees and checks that every channel of the facility profile
     (valve cmd/read, pumps, gauges, turbo lines) exists on the named module;
  2. opens the INPUT tasks only (no digital-output task is created or reserved) and prints the live
     readings for a while: gauge volts → Torr / mBar, valve reed switches, primary/chiller feedback,
     turbo error line and speed.
Nothing is written, so the facility stays exactly as it is.  Compare the numbers with the LabVIEW
front panel (LabVIEW must be closed while this runs – DAQmx lets only one program own the lines).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from facility_control.config import load_config  # noqa: E402
from facility_control.units import torr_to  # noqa: E402


def channel_inventory(cfg):
    """(kind, phys, description) for every configured channel."""
    items = []
    for vid, v in cfg.valves.items():
        items.append(("do", cfg.phys(v.cmd), f"{v.label} command"))
        items.append(("di", cfg.phys(v.read), f"{v.label} read (reed)"))
    for pump in (cfg.primary, cfg.chiller):
        if pump.two_stage:
            items.append(("do", cfg.phys(pump.power_cmd), f"{pump.label} power relay"))
            items.append(("do", cfg.phys(pump.run_cmd), f"{pump.label} run relay"))
        else:
            items.append(("do", cfg.phys(pump.cmd), f"{pump.label} command"))
        if pump.has_read:
            items.append(("di", cfg.phys(pump.read), f"{pump.label} read"))
        if pump.has_frequency:
            items.append(("ai", cfg.phys(pump.frequency.channel),
                          f"{pump.label} frequency (0-{pump.frequency.max_hz:g} Hz)"))
    for g in cfg.gauges:
        items.append(("ai", cfg.phys(g.channel), f"{g.label} ({g.formula})"))
    for ea in cfg.extra_analog:
        items.append(("ai", cfg.phys(ea["channel"]), ea["label"]))
    if cfg.compressor_ai:
        items.append(("ai", cfg.phys(cfg.compressor_ai), "Compressor pressure"))
    for tc in cfg.turbos:
        p = tc.params
        for key, kind in (("motor_do", "do"), ("standby_do", "do"), ("error_ack_do", "do"),
                          ("error_di", "di"), ("still_spinning_di", "di"), ("speed_ai", "ai"),
                          ("rotating_di", "di"), ("accelerating_di", "di"), ("at_speed_di", "di"),
                          ("braking_di", "di"), ("alarm_di", "di"), ("warning_di", "di")):
            if key in p:
                items.append((kind, cfg.phys(p[key]), f"{tc.label} {key} ({tc.control_mode})"))
    return items


def check_channels(cfg) -> bool:
    import nidaqmx
    from nidaqmx.system import System

    system = System.local()
    print(f"NI-DAQmx driver {system.driver_version.major_version}.{system.driver_version.minor_version}")
    devices = {d.name.lower(): d for d in system.devices}
    print("Devices seen by the driver:")
    for d in system.devices:
        try:
            print(f"  {d.name:14s} {d.product_type}")
        except Exception:
            print(f"  {d.name}")
    if not devices:
        print("  (none) – is the cDAQ powered, connected and visible in NI MAX?")
    ok = True
    print(f"\nChannel check for profile '{cfg.name}' (DAQ name '{cfg.daq_name}'):")
    for kind, phys, desc in channel_inventory(cfg):
        dev_name = phys.split("/", 1)[0].lower()
        dev = devices.get(dev_name)
        if dev is None:
            status = f"MISSING module {phys.split('/', 1)[0]}"
            ok = False
        else:
            try:
                coll = {"do": dev.do_lines, "di": dev.di_lines, "ai": dev.ai_physical_chans}[kind]
                names = {c.name.lower() for c in coll}
                if phys.lower() in names:
                    status = "ok"
                else:
                    status = "MISSING line/channel"
                    ok = False
            except Exception as exc:
                status = f"? ({exc})"
        print(f"  {kind.upper():2s} {phys:28s} {status:22s} {desc}")
    return ok


def live_readings(cfg, seconds: float) -> None:
    from facility_control.hal.nidaqmx_backend import NiDaqmxBackend

    backend = NiDaqmxBackend(cfg, read_only=True)
    backend.open()
    print(f"\nLive readings ({backend.describe()}) for {seconds:.0f} s – Ctrl+C to stop.  Nothing is written.")
    t_end = time.time() + seconds
    try:
        while time.time() < t_end:
            t0 = time.time()
            inp = backend.read()
            dt = time.time() - t0
            if inp.daq_error:
                print(f"  DAQ error: {inp.daq_error}")
                time.sleep(1.0)
                continue
            parts = []
            for g in cfg.gauges:
                p = inp.pressures_torr.get(g.id, float("nan"))
                parts.append(f"{g.id}={inp.gauge_volts.get(g.id, float('nan')):.3f}V "
                             f"{p:.2e}Torr {torr_to('mbar', p):.2e}mBar")
            for ea in cfg.extra_analog:
                parts.append(f"{ea['id']}={inp.extra_analog.get(ea['id'], float('nan')):.3f}V")
            if inp.compressor_bar is not None:
                parts.append(f"compressor={inp.compressor_bar:.2f}")
            valves = " ".join(f"{vid}={'OPEN' if inp.valve_reads.get(vid) else 'closed'}" for vid in cfg.valve_ids)
            pumps = f"primary={'ON' if inp.primary_read else 'off'}"
            if inp.primary_hz is not None:
                pumps += f" ({inp.primary_hz:.1f} Hz)"
            pumps += f" chiller={'ON' if inp.chiller_read else 'off'}"
            turbos = " ".join(
                (f"{tid}: " + " ".join(f"{k}={'Y' if v else 'n'}" for k, v in ti.contacts.items()))
                if ti.contacts else
                (f"{tid}: err={'YES' if ti.error else 'no'} speed={ti.speed_pct:.0f}%"
                 + (f" spinning={ti.still_spinning}" if ti.still_spinning else ""))
                for tid, ti in inp.turbos.items())
            print(time.strftime("%H:%M:%S"), f"(read {dt * 1000:.0f} ms)")
            print("   ", " | ".join(parts))
            print("   ", valves, "|", pumps, "|", turbos)
            time.sleep(max(0.0, 1.0 - dt))
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", "-c", default=None)
    ap.add_argument("--seconds", type=float, default=20.0, help="how long to print live readings (0 = channel check only)")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    try:
        import nidaqmx  # noqa: F401
    except ImportError:
        print("The 'nidaqmx' package is not installed in this Python environment.\n"
              "  %LOCALAPPDATA%\\FacilityControl\\venv\\Scripts\\pip install nidaqmx\n"
              "and the NI-DAQmx driver (ni.com) must be installed on this PC.", file=sys.stderr)
        return 2
    try:
        ok = check_channels(cfg)
    except Exception as exc:
        print(f"Could not query the NI-DAQmx driver: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if not ok:
        print("\nSome channels are missing – fix the channel map / device name in the YAML (or the module "
              "names in NI MAX) before running the controller.")
    if args.seconds > 0:
        live_readings(cfg, args.seconds)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
