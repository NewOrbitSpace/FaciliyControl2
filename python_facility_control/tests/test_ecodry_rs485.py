"""The ECODRY plus X104 Modbus RTU tool, against a simulated pump on a fake serial port.

No hardware and no pyserial needed: a fake port answers Modbus RTU frames from a register bank,
so the framing, CRC, scaling, safety behaviour and the register map of tools/ecodry_rs485.py are
all exercised offline.  Register numbers and semantics come from Operating Instructions
300758785_002_C1 section 4.2.2.
"""
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from ecodry_rs485 import (DRIVE_STATUS, EcoDry, ModbusError, NOMINAL_HZ, R_CMD_SOURCE, R_DRIVE_STATUS,
                          R_ERROR_CODE, R_FREQ_OUT, R_FREQ_SET, R_RESET, R_RUN_CMD, RUN_FWD,
                          RtuClient, STOP_FWD, build_frame, crc16)


# ---------------------------------------------------------------------------- fake pump
class FakePump:
    """Answers Modbus RTU function 3 / 6 from a register bank, like the drive would."""

    def __init__(self, slave=1, corrupt_crc=False, silent=False):
        self.slave, self.corrupt_crc, self.silent = slave, corrupt_crc, silent
        self.regs = {0x2103: 0, 0x2102: 0, 0x2101: 0b00, 0x2100: 0, 0x2104: 150, 0x2106: 2300,
                     0x2105: 3200, 0x210F: 90, 0x220E: 41, 0x0520: 12, 0x051F: 334, 0x0006: 102,
                     0x0015: 1, 0x2000: STOP_FWD, 0x0400: 0}
        self.writes = []
        self._out = b""
        self.is_open = True

    # -- the plant it models: running only when RS485 has control and RUN was written
    def _update(self):
        running = self.regs[0x0015] == 2 and (self.regs[0x2000] & 0b11) == 0b10
        self.regs[0x2103] = self.regs[0x0400] if running else 0
        self.regs[0x2102] = self.regs[0x0400]
        self.regs[0x2101] = 0b11 if running else 0b00

    # -- pyserial-ish surface
    def reset_input_buffer(self): self._out = b""
    def close(self): self.is_open = False

    def read(self, n=1):
        data, self._out = self._out[:n], self._out[n:]
        return data

    def write(self, frame):
        if self.silent:
            return len(frame)
        assert crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0], "master sent a bad CRC"
        slave, func, body = frame[0], frame[1], frame[2:-2]
        if slave != self.slave:
            return len(frame)
        if func == 3:
            addr, count = struct.unpack(">HH", body)
            self._update()
            vals = b"".join(struct.pack(">H", self.regs.get(addr + i, 0)) for i in range(count))
            payload = bytes([slave, 3, 2 * count]) + vals
        elif func == 6:
            addr, value = struct.unpack(">HH", body)
            self.regs[addr] = value
            self.writes.append((addr, value))
            payload = bytes([slave, 6]) + body
        else:
            payload = bytes([slave, func | 0x80, 1])
        crc = crc16(payload) ^ (0xFFFF if self.corrupt_crc else 0)
        self._out = payload + struct.pack("<H", crc)
        return len(frame)


@pytest.fixture
def pump():
    fake = FakePump()
    client = RtuClient(port=None, slave=1, serial_obj=fake)
    client.frame_gap = 0.0
    p = EcoDry(client)
    p.fake = fake
    return p


# ---------------------------------------------------------------------------- protocol
def test_crc_matches_the_canonical_modbus_check_value():
    assert crc16(b"123456789") == 0x4B37


def test_frames_are_well_formed():
    f = build_frame(1, 3, struct.pack(">HH", R_FREQ_OUT, 1))
    assert f.hex() == "01032103000" + f.hex()[11:]      # slave, function, address
    assert crc16(f[:-2]) == struct.unpack("<H", f[-2:])[0]


def test_read_reports_scaled_values(pump):
    pump.fake.regs.update({0x2103: 21000, 0x2102: 21000, 0x2101: 0b11, 0x2104: 275, 0x210F: 105})
    pump.fake._update = lambda: None                    # freeze the fake plant
    t = pump.read_all()
    assert t.freq_out_hz == pytest.approx(210.0)        # 21000 counts / 100 = 210 Hz
    assert t.current_a == pytest.approx(2.75)
    assert t.power_kw == pytest.approx(1.05)
    assert t.status == "operating" and t.running
    assert t.error_code == 0 and t.runtime_days == 12


def test_error_codes_are_decoded(pump):
    for code, fragment in ((13, "under-voltage"), (21, "overload"), (16, "overheat"), (1, "overcurrent")):
        pump.fake.regs[R_ERROR_CODE] = code
        assert fragment in pump.read_all().error_text


def test_bad_crc_is_reported_not_silently_accepted():
    client = RtuClient(port=None, slave=1, serial_obj=FakePump(corrupt_crc=True))
    client.frame_gap = 0.0
    with pytest.raises(ModbusError, match="CRC"):
        EcoDry(client).read_all()


def test_no_reply_is_reported(pump):
    client = RtuClient(port=None, slave=1, serial_obj=FakePump(silent=True), timeout=0.01)
    client.frame_gap = 0.0
    with pytest.raises(ModbusError, match="no reply"):
        EcoDry(client).read_all()


# ---------------------------------------------------------------------------- control
def test_run_needs_the_rs485_command_source(pump):
    """Writing RUN while X1 still owns the pump must not start it - the manual's read-only mode."""
    pump.run()
    assert pump.read_all().status == "stopped"          # 0015H is still 1 (external terminals)
    pump.take_control()
    pump.set_frequency(NOMINAL_HZ)
    pump.run()
    t = pump.read_all()
    assert t.status == "operating" and t.freq_out_hz == pytest.approx(NOMINAL_HZ)


def test_run_and_stop_write_the_documented_bit_patterns(pump):
    pump.take_control()
    pump.run(); pump.stop()
    assert (R_CMD_SOURCE, 2) in pump.fake.writes
    assert (R_RUN_CMD, RUN_FWD) in pump.fake.writes and (R_RUN_CMD, STOP_FWD) in pump.fake.writes
    assert RUN_FWD == 0x12 and STOP_FWD == 0x11         # bits 1..0 run/stop, bits 5..4 = 01B FWD


def test_frequency_is_scaled_and_bounded(pump):
    pump.set_frequency(105.0)
    assert (R_FREQ_SET, 10500) in pump.fake.writes
    for bad in (-1.0, NOMINAL_HZ + 0.1, 400.0):
        with pytest.raises(ValueError):
            pump.set_frequency(bad)


def test_reset_sets_bit_1(pump):
    pump.reset_fault()
    assert (R_RESET, 0x0002) in pump.fake.writes


# ---------------------------------------------------------------------------- safety
def test_safe_exit_stops_the_pump_and_restores_the_x1_relay(pump):
    pump.take_control()
    pump.set_frequency(NOMINAL_HZ)
    pump.run()
    assert pump.read_all().running
    pump.safe_exit()
    assert pump.fake.regs[R_RUN_CMD] == STOP_FWD        # pump stopped
    assert pump.fake.regs[R_CMD_SOURCE] == 1            # X1 run relay works again
    assert not pump.read_all().running


def test_safe_exit_still_restores_control_if_the_stop_write_fails(pump):
    pump.take_control()
    pump.run()
    calls = {"n": 0}
    real = pump.c.write_single

    def flaky(addr, value):
        if addr == R_RUN_CMD and calls["n"] == 0:
            calls["n"] += 1
            raise ModbusError("line noise")
        return real(addr, value)
    pump.c.write_single = flaky
    pump.safe_exit()                                     # must not raise
    assert pump.fake.regs[R_CMD_SOURCE] == 1             # X1 handed back despite the failed stop


def test_keep_control_leaves_rs485_in_charge(pump):
    pump.take_control()
    pump.safe_exit(keep_control=True)
    assert pump.fake.regs[R_CMD_SOURCE] == 2


def test_control_subcommand_refuses_without_allow_control():
    from ecodry_rs485 import main
    with pytest.raises(SystemExit) as e:
        main(["--port", "COM_NONE", "control", "--run"])
    assert e.value.code != 0


def test_drive_status_table_matches_the_manual():
    assert DRIVE_STATUS == {0b00: "stopped", 0b01: "decelerating", 0b10: "standby", 0b11: "operating"}
