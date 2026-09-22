"""Leybold ECODRY plus - X104 RS-485 (Modbus RTU) interface.

Standalone bring-up and control tool for the small chamber's primary pump, written against
Operating Instructions 300758785_002_C1 section 4.2.2.  It is deliberately independent of the
facility control program: prove the cable, the converter and the protocol here first, then the
same `EcoDry` class can be dropped into the HAL.

    python tools/ecodry_rs485.py --port COM5 dump          # read every register once
    python tools/ecodry_rs485.py --port COM5 watch         # live, 1 Hz, read-only
    python tools/ecodry_rs485.py --port COM5 --allow-control control --run --freq 210
    python tools/ecodry_rs485.py --port COM5 --allow-control control --stop --release

SERIAL / WIRING (manual section 4.2.2, table "Pin assignment serial interface X104")
    9600 baud, 8 data bits, no parity, 1 stop bit, slave address 1.
    Your cable needs a MALE SUB-D 9 (X104 on the pump is female):
        pin 7  Tx/Rx +  (A)          pin 5  GND
        pin 8  Tx/Rx -  (B)          pins 2-3 linked -> pump's internal termination
        housing  cable shield, grounded at the CONVERTER end only
    Use a galvanically isolated USB<->RS-485 converter and keep the cable away from the pump's
    mains and motor wiring: the drive is a VFD and this is a long unbalanced-ish run.

!! SAFETY -- read before using `control` !!
    The manual states that while the X1 control interface commands the pump, X104 is READ-ONLY.
    Taking control means writing 0015H = 2 (RS485), which DISABLES the X1 run relay -- the
    hardwired stop the facility program relies on.  While this tool holds control the only
    independent way to stop the pump is removing mains (the power relay).
    This tool therefore:
      * refuses every write unless --allow-control is given,
      * stops the pump and hands control back to the X1 terminals on exit, Ctrl-C included,
      * never leaves the command source on RS485 after a clean exit unless --keep is given.
"""
from __future__ import annotations

import argparse
import signal
import struct
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


# --------------------------------------------------------------------------- Modbus RTU
def crc16(payload: bytes) -> int:
    """Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF), returned host-order."""
    crc = 0xFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_frame(slave: int, function: int, body: bytes) -> bytes:
    core = bytes([slave, function]) + body
    return core + struct.pack("<H", crc16(core))


class ModbusError(RuntimeError):
    pass


EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
              4: "slave device failure", 5: "acknowledge", 6: "slave device busy",
              8: "memory parity error", 10: "gateway path unavailable", 11: "gateway target failed"}


class RtuClient:
    """Minimal Modbus RTU master over pyserial - functions 3 (read holding) and 6 (write single).

    Implemented here rather than pulled from a library so the lab PC needs only pyserial, and so
    the framing can be unit-tested against a loopback with no hardware present.
    """

    def __init__(self, port: str, slave: int = 1, baud: int = 9600, timeout: float = 0.5,
                 serial_obj=None):
        self.slave = slave
        if serial_obj is not None:
            self.ser = serial_obj
        else:
            import serial  # imported lazily so --help works without pyserial
            self.ser = serial.Serial(port=port, baudrate=baud, bytesize=8,
                                     parity="N", stopbits=1, timeout=timeout)
        # 3.5 character times of silence between frames (Modbus RTU); 9600 8N1 -> ~4 ms
        self.frame_gap = max(0.004, 3.5 * 11 / baud)

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def _transact(self, function: int, body: bytes, expect: int) -> bytes:
        frame = build_frame(self.slave, function, body)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        time.sleep(self.frame_gap)
        self.ser.write(frame)
        head = self.ser.read(2)
        if len(head) < 2:
            raise ModbusError("no reply (check port, wiring A/B, baud, slave address)")
        if head[0] != self.slave:
            raise ModbusError(f"reply from slave {head[0]}, expected {self.slave}")
        if head[1] & 0x80:                      # exception response
            rest = self.ser.read(3)
            code = rest[0] if rest else 0
            raise ModbusError(f"modbus exception {code}: {EXCEPTIONS.get(code, 'unknown')}")
        if head[1] != function:
            raise ModbusError(f"function {head[1]} in reply, expected {function}")
        rest = self.ser.read(expect + 2)        # payload + CRC
        if len(rest) < expect + 2:
            raise ModbusError(f"short reply: {len(rest)} of {expect + 2} bytes")
        payload, got = rest[:expect], struct.unpack("<H", rest[expect:expect + 2])[0]
        if crc16(head + payload) != got:
            raise ModbusError("CRC mismatch (noise on the line, or missing termination)")
        return payload

    def read_holding(self, address: int, count: int = 1) -> List[int]:
        body = struct.pack(">HH", address, count)
        payload = self._transact(3, body, 1 + 2 * count)
        if payload[0] != 2 * count:
            raise ModbusError(f"byte count {payload[0]}, expected {2 * count}")
        return [struct.unpack(">H", payload[1 + 2 * i:3 + 2 * i])[0] for i in range(count)]

    def write_single(self, address: int, value: int) -> None:
        body = struct.pack(">HH", address, value & 0xFFFF)
        echo = self._transact(6, body, 4)
        if echo != body:
            raise ModbusError(f"write not echoed: sent {body.hex()}, got {echo.hex()}")


# --------------------------------------------------------------------------- register map
# Operating Instructions 300758785_002_C1, section 4.2.2 "X104 Serial RS-485 Interface".
R_RUN_CMD        = 0x2000   # W  bits 1..0: 01B stop, 10B run; bits 5..4 always 01B (FWD)
R_RESET          = 0x2002   # W  set bit 1 to 1
R_CMD_SOURCE     = 0x0015   # W  1 = external terminals (X1), 2 = RS485 communication
R_FREQ_SET       = 0x0400   # W  frequency command
R_ERROR_CODE     = 0x2100   # R
R_DRIVE_STATUS   = 0x2101   # R  bits 1..0: 00 stopped, 01 decelerating, 10 standby, 11 operating
R_FREQ_CMD       = 0x2102   # R  frequency command read-back
R_FREQ_OUT       = 0x2103   # R  output frequency
R_CURRENT        = 0x2104   # R  output current (A)
R_DC_BUS         = 0x2105   # R  DC bus voltage (V)
R_VOLTAGE        = 0x2106   # R  output voltage (V)
R_POWER          = 0x210F   # R  output power (kW)
R_TEMPERATURE    = 0x220E   # R  drive power module temperature (degC)
R_RUNTIME_MIN    = 0x051F   # R  accumulated run time (minutes)
R_RUNTIME_DAYS   = 0x0520   # R  accumulated run time (days)
R_SW_VERSION     = 0x0006   # R  software revision

RUN_FWD  = 0b10 | (0b01 << 4)    # 0x12 - run, forward
STOP_FWD = 0b01 | (0b01 << 4)    # 0x11 - stop, forward

DRIVE_STATUS = {0b00: "stopped", 0b01: "decelerating", 0b10: "standby", 0b11: "operating"}

# section 6 "Troubleshooting", the codes the manual lists against MODBUS
ERROR_CODES = {0: "no error", 1: "ocA - overcurrent during acceleration",
               2: "ocd - overcurrent during deceleration", 3: "ocn - overcurrent at constant speed",
               8: "ovd - DC over-voltage", 9: "ovn - DC over-voltage",
               13: "Lvn - DC under-voltage", 14: "LvS - controller under-voltage / DC charge fault",
               16: "oH1 - heatsink overheat", 21: "oL - motor overload",
               22: "EoL1 - motor overload", 24: "oH3 - heatsink overheat"}

NOMINAL_HZ = 210.0          # 12 600 rpm; also 10 V on the X1 analog frequency monitor
LOW_SPEED_HZ = 170.0        # X1 pin 2 bridged to pin 4


@dataclass
class Telemetry:
    raw: Dict[str, int]
    freq_out_hz: float
    freq_cmd_hz: float
    status: str
    error_code: int
    error_text: str
    current_a: float
    voltage_v: float
    dc_bus_v: float
    power_kw: float
    temperature_c: float
    runtime_days: int
    runtime_minutes: int

    @property
    def running(self) -> bool:
        return self.status == "operating"


class EcoDry:
    """The pump as seen over X104.

    `freq_scale` is how many raw counts make 1 Hz.  The manual only says "Frequency Command (Hz)"
    without stating the resolution; this drive family conventionally uses 0.01 Hz (scale 100), which
    is the default.  `dump` prints raw counts alongside the scaled values so the scale can be
    confirmed against the pump's own display before anything is written -- do that first.
    """

    def __init__(self, client: RtuClient, freq_scale: float = 100.0):
        self.c = client
        self.freq_scale = freq_scale
        self._took_control = False
        self._started = False

    # ---------------------------------------------------------------- read
    def read_all(self) -> Telemetry:
        raw: Dict[str, int] = {}
        for name, reg in (("freq_out", R_FREQ_OUT), ("freq_cmd", R_FREQ_CMD), ("status", R_DRIVE_STATUS),
                          ("error", R_ERROR_CODE), ("current", R_CURRENT), ("voltage", R_VOLTAGE),
                          ("dc_bus", R_DC_BUS), ("power", R_POWER), ("temperature", R_TEMPERATURE),
                          ("runtime_days", R_RUNTIME_DAYS), ("runtime_min", R_RUNTIME_MIN)):
            raw[name] = self.c.read_holding(reg)[0]
        err = raw["error"]
        return Telemetry(
            raw=raw,
            freq_out_hz=raw["freq_out"] / self.freq_scale,
            freq_cmd_hz=raw["freq_cmd"] / self.freq_scale,
            status=DRIVE_STATUS.get(raw["status"] & 0b11, "unknown"),
            error_code=err, error_text=ERROR_CODES.get(err, f"undocumented code {err}"),
            current_a=raw["current"] / 100.0, voltage_v=raw["voltage"] / 10.0,
            dc_bus_v=raw["dc_bus"] / 10.0, power_kw=raw["power"] / 100.0,
            temperature_c=float(raw["temperature"]),
            runtime_days=raw["runtime_days"], runtime_minutes=raw["runtime_min"])

    # ---------------------------------------------------------------- write
    def take_control(self) -> None:
        """Command source -> RS485.  This DISABLES the X1 run relay (see the module docstring)."""
        self.c.write_single(R_CMD_SOURCE, 2)
        self._took_control = True

    def release_control(self) -> None:
        """Command source -> external terminals, restoring the X1 run relay."""
        self.c.write_single(R_CMD_SOURCE, 1)
        self._took_control = False

    def run(self) -> None:
        self.c.write_single(R_RUN_CMD, RUN_FWD)
        self._started = True

    def stop(self) -> None:
        self.c.write_single(R_RUN_CMD, STOP_FWD)
        self._started = False

    def set_frequency(self, hz: float) -> None:
        if not 0.0 <= hz <= NOMINAL_HZ:
            raise ValueError(f"frequency {hz} Hz outside 0..{NOMINAL_HZ:g} Hz")
        self.c.write_single(R_FREQ_SET, int(round(hz * self.freq_scale)))

    def reset_fault(self) -> None:
        self.c.write_single(R_RESET, 0x0002)

    def safe_exit(self, keep_control: bool = False) -> None:
        """Always called on the way out: stop the pump, hand control back to the X1 terminals."""
        try:
            if self._started:
                self.stop()
        except Exception as exc:
            print(f"  !! could not stop the pump over RS485: {exc}", file=sys.stderr)
            print("  !! remove mains (the power relay) if the pump is still running", file=sys.stderr)
        if self._took_control and not keep_control:
            try:
                self.release_control()
                print("  control handed back to the X1 terminals")
            except Exception as exc:
                print(f"  !! could not restore the X1 command source: {exc}", file=sys.stderr)
                print("  !! the X1 run relay may still be disabled - write 0015H = 1 to restore it",
                      file=sys.stderr)


# --------------------------------------------------------------------------- CLI
def _fmt(t: Telemetry) -> str:
    return (f"{t.freq_out_hz:6.2f} Hz out (cmd {t.freq_cmd_hz:6.2f})  {t.status:12s} "
            f"{t.current_a:5.2f} A  {t.power_kw:5.2f} kW  {t.dc_bus_v:5.1f} Vdc  "
            f"{t.temperature_c:4.0f} C  err {t.error_code}"
            + ("" if t.error_code == 0 else f" ({t.error_text})"))


def cmd_dump(pump: EcoDry, args) -> int:
    t = pump.read_all()
    print("ECODRY plus - X104 Modbus RTU\n")
    print(f"  {'register':28s} {'raw':>8s}   scaled")
    rows = [("2103H output frequency", t.raw["freq_out"], f"{t.freq_out_hz:.2f} Hz"),
            ("2102H frequency command", t.raw["freq_cmd"], f"{t.freq_cmd_hz:.2f} Hz"),
            ("2101H drive status", t.raw["status"], t.status),
            ("2100H error code", t.raw["error"], t.error_text),
            ("2104H output current", t.raw["current"], f"{t.current_a:.2f} A"),
            ("2106H output voltage", t.raw["voltage"], f"{t.voltage_v:.1f} V"),
            ("2105H DC bus voltage", t.raw["dc_bus"], f"{t.dc_bus_v:.1f} V"),
            ("210FH output power", t.raw["power"], f"{t.power_kw:.2f} kW"),
            ("220EH module temperature", t.raw["temperature"], f"{t.temperature_c:.0f} C"),
            ("0520H run time (days)", t.raw["runtime_days"], f"{t.runtime_days} d"),
            ("051FH run time (minutes)", t.raw["runtime_min"], f"{t.runtime_minutes} min")]
    for name, raw, scaled in rows:
        print(f"  {name:28s} {raw:8d}   {scaled}")
    try:
        print(f"  {'0006H software revision':28s} {pump.c.read_holding(R_SW_VERSION)[0]:8d}")
    except ModbusError as exc:
        print(f"  0006H software revision      -- ({exc})")
    print(f"\n  frequency scale assumed: {pump.freq_scale:g} counts per Hz.")
    print("  CONFIRM THIS against the pump's own display before writing a frequency:")
    print(f"  with the pump at full speed the display reads H210.0 and 2103H should read "
          f"{int(NOMINAL_HZ * pump.freq_scale)}.")
    if t.raw["freq_out"] and abs(t.freq_out_hz - NOMINAL_HZ) > 1.0 and t.status == "operating":
        print(f"  !! it currently reads {t.freq_out_hz:.2f} Hz - if the display says {NOMINAL_HZ:g}, "
              f"rerun with --freq-scale {t.raw['freq_out'] / NOMINAL_HZ:.0f}")
    return 0


def cmd_watch(pump: EcoDry, args) -> int:
    print("live telemetry (read-only), Ctrl-C to stop\n")
    t_end = time.time() + args.seconds if args.seconds else None
    while t_end is None or time.time() < t_end:
        try:
            print(f"  {time.strftime('%H:%M:%S')}  {_fmt(pump.read_all())}")
        except ModbusError as exc:
            print(f"  {time.strftime('%H:%M:%S')}  -- {exc}")
        time.sleep(args.interval)
    return 0


def cmd_control(pump: EcoDry, args) -> int:
    print("!! taking command of the pump over RS485.")
    print("!! the X1 run relay (Mod3/port0/line7) will NOT stop the pump while this holds control.")
    print("!! the only independent stop is removing mains (the power relay, Mod3/port0/line6).\n")
    pump.take_control()
    print("  0015H = 2 (RS485 command source)")
    if args.reset:
        pump.reset_fault()
        print("  2002H fault reset pulsed")
    if args.freq is not None:
        pump.set_frequency(args.freq)
        print(f"  0400H = {int(round(args.freq * pump.freq_scale))} ({args.freq:g} Hz)")
    if args.run:
        pump.run()
        print("  2000H = RUN")
        deadline = time.time() + args.spinup_timeout
        while time.time() < deadline:
            t = pump.read_all()
            print(f"    {_fmt(t)}")
            if t.error_code:
                print(f"  !! drive reports error {t.error_code}: {t.error_text} - stopping")
                return 2
            if t.running and abs(t.freq_out_hz - t.freq_cmd_hz) < 1.0:
                print("  at commanded frequency")
                break
            time.sleep(1.0)
        else:
            print(f"  !! not at commanded frequency within {args.spinup_timeout:g} s")
    if args.stop:
        pump.stop()
        print("  2000H = STOP")
    if args.hold:
        print(f"\n  holding for {args.hold:g} s - Ctrl-C stops the pump and releases control")
        t_end = time.time() + args.hold
        while time.time() < t_end:
            print(f"    {_fmt(pump.read_all())}")
            time.sleep(args.interval)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="serial port of the RS-485 converter (COM5, /dev/ttyUSB0)")
    ap.add_argument("--slave", type=int, default=1, help="Modbus address (manual: 1)")
    ap.add_argument("--baud", type=int, default=9600, help="manual: 9600 8N1")
    ap.add_argument("--timeout", type=float, default=0.5)
    ap.add_argument("--freq-scale", type=float, default=100.0,
                    help="raw counts per Hz (default 100 = 0.01 Hz resolution) - confirm with `dump`")
    ap.add_argument("--allow-control", action="store_true",
                    help="required for any write; without it the tool is strictly read-only")
    ap.add_argument("--keep", action="store_true",
                    help="leave the command source on RS485 at exit (default: hand back to X1)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("dump", help="read every documented register once")

    w = sub.add_parser("watch", help="live telemetry, read-only")
    w.add_argument("--interval", type=float, default=1.0)
    w.add_argument("--seconds", type=float, default=None, help="stop after N seconds")

    c = sub.add_parser("control", help="command the pump (needs --allow-control)")
    c.add_argument("--run", action="store_true")
    c.add_argument("--stop", action="store_true")
    c.add_argument("--freq", type=float, default=None, metavar="HZ", help=f"0..{NOMINAL_HZ:g}")
    c.add_argument("--reset", action="store_true", help="clear a drive fault first")
    c.add_argument("--release", action="store_true", help="hand control back and exit")
    c.add_argument("--hold", type=float, default=0.0, metavar="SECONDS", help="keep running and report")
    c.add_argument("--interval", type=float, default=2.0)
    c.add_argument("--spinup-timeout", type=float, default=30.0)

    args = ap.parse_args(argv)
    if args.cmd == "control" and not args.allow_control:
        ap.error("`control` writes to the pump: pass --allow-control once you have read the safety "
                 "note at the top of this file")

    try:
        client = RtuClient(args.port, slave=args.slave, baud=args.baud, timeout=args.timeout)
    except Exception as exc:
        print(f"cannot open {args.port}: {exc}", file=sys.stderr)
        return 1
    pump = EcoDry(client, freq_scale=args.freq_scale)

    interrupted = {"flag": False}

    def on_signal(_sig, _frm):
        interrupted["flag"] = True
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, on_signal)

    try:
        if args.cmd == "dump":
            return cmd_dump(pump, args)
        if args.cmd == "watch":
            return cmd_watch(pump, args)
        return cmd_control(pump, args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    except ModbusError as exc:
        print(f"\nmodbus error: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.cmd == "control":
            print("\nshutting down safely:")
            pump.safe_exit(keep_control=args.keep and not interrupted["flag"])
        client.close()


if __name__ == "__main__":
    sys.exit(main())
