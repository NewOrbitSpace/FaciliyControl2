"""The RS-485 link to the HiPace turbos must look after itself (2026-11-27).

What was wrong.  Everything that talks to the pumps runs in one background thread per serial port
(hal/hipace_rs485_turbos.py).  The code caught the pump protocol's own errors (no reply, bad
checksum), but not an error of the PORT - what pyserial raises when the USB/RS-485 converter drops
out for a moment ('WriteFile failed', 'ClearCommError failed', 'device disconnected').  One such
error went straight up and ended the thread.  Nothing restarted it: from then on no reading, no
re-opening of the port, and no stop command either, until the program was restarted.  The
operators saw "No RS-485 reply" that never went away.

What it does now.
  * a port error is an ordinary PVError (PVPortError): nothing above it falls over;
  * the port is closed at once (Windows cannot give a converter its COM number back while a dead
    handle is held) and opened again - every second for the first minute, every 5 s after that,
    for as long as the program runs;
  * when it is back, RS-485 control is taken again and the wanted state (run / stop / standby /
    speed) is sent again;
  * if the SAME converter comes back under another COM number, it is followed (and the log says
    which number to put in the profile); `usb_id:` in the profile finds it at start-up too;
  * the poller cannot end: anything unexpected is logged and it carries on, and the controller's
    loop starts a poller that is not running;
  * a fault that lasts writes one log line, then one every ten minutes - not eight every 10 s.
"""
import time

import pytest

from test_hipace_rs485_facility import _bus, _turbos
from facility_control.drivers.pfeiffer_pv import HiPace, PVBus, PVError, PVPortError, PVTimeout
from facility_control.hal.hipace_rs485_turbos import RS485Turbos

USB = {"vid": 0x0403, "pid": 0x6001, "serial": "A9XK2"}


class Port:
    """A serial port on a USB converter that can be unplugged.  `world` is shared between the
    handles: {"present": {name: sim bus}}.  A handle opened before an unplug never works again."""

    def __init__(self, world, name):
        self.world, self.name, self.sim = world, name, world["present"][name]
        self.generation = world["generation"]
        self.closed = False

    def _check(self):
        if self.closed or self.generation != self.world["generation"] or self.name not in self.world["present"]:
            raise OSError("WriteFile failed (PermissionError(13, 'Access is denied.', None, 5))")

    def reset_input_buffer(self):
        self.sim.reset_input_buffer()

    def write(self, data):
        self._check()
        return self.sim.write(data)

    def read_until(self, terminator=b"\r"):
        self._check()
        if self.sim.dead:
            return b""                                      # (the simulator would sleep 20 ms here)
        return self.sim.read_until(terminator)

    def close(self):
        self.closed = True
        self.world["closed"] += 1


def rig(n=2, name="COM4", lister=True, start_threads=False, **params):
    """-> (manager, world, sim bus, clock list).  The clock is the test's own unless threads run."""
    sim = _bus(n)
    world = {"present": {name: sim}, "generation": 0, "closed": 0, "opens": []}
    t = [1000.0]

    def factory(port, baud, timeout):
        world["opens"].append(port)
        if port not in world["present"]:
            raise OSError(f"could not open port '{port}': FileNotFoundError(2, 'The system cannot find "
                          f"the file specified.', None, 2)")
        return Port(world, port)

    def ports():
        out = [{"device": "COM1", "description": "Communications Port", "vid": None, "pid": None, "serial": ""}]
        out += [dict(USB, device=d, description="USB Serial Port") for d in world["present"]]
        return out + list(world.get("extra", []))

    turbos = _turbos(n, port=name)
    for x in turbos:
        x.params.update({"port_reset_s": 0.0, "comm_timeout_s": 20.0, "comm_warn_s": 3.0})
        x.params.update(params)
    m = RS485Turbos(turbos, serial_factory=factory, port_lister=ports if lister else None,
                    **({} if start_threads else {"clock": lambda: t[0]}))
    m.open(start_threads=start_threads)
    for b in m.buses.values():
        b.gap_s = 0.0
    return m, world, sim, t


def unplug(world, name="COM4"):
    world["generation"] += 1
    return world["present"].pop(name)


# ---------------------------------------------------------------------------- the driver
def test_a_failing_port_is_a_pv_error_not_whatever_the_operating_system_threw():
    world = {"present": {"COM4": _bus(1)}, "generation": 0, "closed": 0}
    bus = PVBus("COM4", serial_obj=Port(world, "COM4"), timeout=0.05)
    bus.gap_s = 0.0
    pump = HiPace(bus, 1)
    assert pump.read(309) is not None
    unplug(world)
    with pytest.raises(PVPortError) as err:
        pump.read(309)
    assert isinstance(err.value, PVError) and not isinstance(err.value, PVTimeout)
    assert "WriteFile failed" in str(err.value) and "WriteFile failed" in bus.port_fault
    st = bus.stats_for(1)
    assert st.port_errors == 1 and st.consecutive_failures == 1 and "1 port errors" in st.summary()


# ---------------------------------------------------------------------------- drop-out and return
def test_a_port_that_drops_out_is_closed_reopened_and_the_wanted_state_sent_again():
    m, world, sim, t = rig(2)
    m.set_commands({"turbo1": True, "turbo2": True}, {})
    m.poll_once_ordered()
    assert sim.pumps[1].p[10] and sim.pumps[2].p[10] and m.inputs("turbo1").comm_ok
    m.take_events()
    # --- the converter drops off the USB bus
    unplug(world)
    m.poll_once_ordered()
    events = m.take_events()
    assert world["closed"] == 1, "the dead handle must be closed, or Windows keeps the COM number locked"
    assert any("stopped working" in e and "WriteFile failed" in e for e in events), events
    assert "COM4" in m.port_errors
    ti = m.inputs("turbo1")
    assert ti.comm_ok and not ti.error                     # a second of silence is not a lost pump
    t[0] += 4.0
    ti = m.inputs("turbo1")
    assert ti.comm_ok and ti.comm_warning                   # ... 4 s: a warning on the panel
    # --- two seconds without the port: tried again every second
    before = len(world["opens"])
    for _ in range(3):
        t[0] += 1.0
        m.poll_once_ordered()
    assert len(world["opens"]) - before == 3 and "COM4" in m.port_errors
    # --- it comes back; one pump was power-cycled meanwhile and has forgotten that it should run
    world["present"]["COM4"] = sim
    sim.pumps[1].p[10] = False
    t[0] += 1.0
    m.poll_once_ordered()
    events = m.take_events()
    assert any("opened again" in e and "taking the pumps back" in e for e in events), events
    assert "COM4" not in m.port_errors
    m.poll_once_ordered()
    assert sim.pumps[1].p[10] and sim.pumps[2].p[10], "the wanted state must be sent again after a reconnect"
    ti = m.inputs("turbo1")
    assert ti.comm_ok and not ti.comm_warning and not ti.error and ti.rs485["port"] == "COM4"


def test_a_stop_asked_for_while_the_port_was_away_is_sent_when_it_is_back():
    m, world, sim, t = rig(2)
    m.set_commands({"turbo1": True, "turbo2": True}, {})
    m.poll_once_ordered()
    unplug(world)
    m.poll_once_ordered()
    m.set_commands({"turbo1": False, "turbo2": True}, {})   # Auto takes Turbo 1 out meanwhile
    for _ in range(30):
        t[0] += 1.0
        m.poll_once_ordered()
    assert sim.pumps[1].p[10], "(nothing can reach the pump while the port is away)"
    world["present"]["COM4"] = sim
    t[0] += 1.0
    m.poll_once_ordered()
    m.poll_once_ordered()
    assert not sim.pumps[1].p[10] and sim.pumps[2].p[10]


def test_it_never_gives_up_every_second_for_a_minute_then_every_five_seconds():
    m, world, sim, t = rig(1)
    m.poll_once_ordered()
    unplug(world)
    m.poll_once_ordered()
    opens0 = len(world["opens"])
    for _ in range(600):                                    # one minute, a pass every 0.1 s
        t[0] += 0.1
        m.poll_once_ordered()
    first_minute = len(world["opens"]) - opens0
    assert 55 <= first_minute <= 62, first_minute
    for _ in range(36000):                                  # one more hour
        t[0] += 0.1
        m.poll_once_ordered()
    hour = len(world["opens"]) - opens0 - first_minute
    assert 700 <= hour <= 725, hour                          # every 5 s
    lines = m.take_events()
    assert 2 <= len(lines) <= 10, lines                      # ... and the log is not full of it
    assert any("still cannot be opened" in e and "still trying" in e for e in lines)
    world["present"]["COM4"] = sim                           # the next morning someone plugs it back in
    t[0] += 5.0
    m.poll_once_ordered()
    m.poll_once_ordered()
    assert m.inputs("turbo1").comm_ok and "COM4" not in m.port_errors


def test_a_port_that_is_missing_at_start_is_tried_again_and_the_message_names_the_ports_there_are():
    sim = _bus(1)
    world = {"present": {}, "generation": 0, "closed": 0, "opens": []}
    t = [50.0]

    def factory(port, baud, timeout):
        world["opens"].append(port)
        if port not in world["present"]:
            raise OSError(f"could not open port '{port}': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)")
        return Port(world, port)

    ports = lambda: [{"device": "COM1", "description": "Communications Port", "vid": None, "pid": None, "serial": ""},
                     dict(USB, device="COM4", description="USB Serial Port")]
    turbos = _turbos(1, port="COM3")
    m = RS485Turbos(turbos, serial_factory=factory, port_lister=ports, clock=lambda: t[0])
    m.open(start_threads=False)
    t[0] += 1.0
    ti = m.inputs("turbo1")
    assert ti.error and not ti.comm_ok
    assert "cannot open COM3" in ti.error_text
    # the operator is told what IS there, with the USB identity to put in the profile
    assert "COM1 (Communications Port)" in ti.error_text and "COM4 (USB Serial Port, USB 0403:6001:A9XK2)" in ti.error_text
    world["present"]["COM3"] = sim
    t[0] += 1.0
    m.poll_once_ordered()
    m.poll_once_ordered()
    assert m.inputs("turbo1").comm_ok


# ---------------------------------------------------------------------------- the same converter, another COM number
def test_the_same_converter_under_another_com_number_is_followed():
    m, world, sim, t = rig(2)
    m.set_commands({"turbo1": True, "turbo2": True}, {})
    m.poll_once_ordered()
    m.take_events()
    unplug(world)
    m.poll_once_ordered()
    world["present"]["COM7"] = sim                           # Windows gave it a new number
    t[0] += 1.0
    m.poll_once_ordered()
    m.poll_once_ordered()
    events = m.take_events()
    assert any("its USB/RS-485 converter is now COM7" in e and "Change `port:` to COM7" in e for e in events), events
    ti = m.inputs("turbo1")
    assert ti.comm_ok and ti.rs485["port"] == "COM7" and sim.pumps[1].p[10]


def test_another_device_is_never_taken_for_the_converter():
    m, world, sim, t = rig(1)
    m.poll_once_ordered()
    unplug(world)
    m.poll_once_ordered()
    # a different USB serial device appears (another instrument)
    world["extra"] = [{"device": "COM8", "description": "Keithley", "vid": 0x05E6, "pid": 0x2400, "serial": "X"},
                      dict(USB, device="COM9", description="USB Serial Port", serial="OTHER")]
    world["present_other"] = True
    opened_before = list(world["opens"])
    for _ in range(10):
        t[0] += 1.0
        m.poll_once_ordered()
    assert set(world["opens"][len(opened_before):]) == {"COM4"}, "only the configured port may be tried"
    assert "COM4" in m.port_errors


def test_two_identical_converters_without_serial_numbers_are_not_guessed_between():
    nos = {"vid": 0x1A86, "pid": 0x7523, "serial": ""}
    sim = _bus(1)
    world = {"present": {"COM4": sim}, "generation": 0, "closed": 0, "opens": []}
    t = [10.0]

    def factory(port, baud, timeout):
        world["opens"].append(port)
        if port not in world["present"]:
            raise OSError("could not open port")
        return Port(world, port)

    ports = lambda: [dict(nos, device=d, description="USB-SERIAL CH340") for d in world["listed"]]
    world["listed"] = ["COM4"]
    m = RS485Turbos(_turbos(1, port="COM4"), serial_factory=factory, port_lister=ports, clock=lambda: t[0])
    m.open(start_threads=False)
    m.poll_once_ordered()
    unplug(world)
    world["listed"] = ["COM5", "COM6"]                        # two of them now, neither is COM4
    m.poll_once_ordered()
    m.take_events()
    for _ in range(5):
        t[0] += 1.0
        m.poll_once_ordered()
    assert set(world["opens"][1:]) == {"COM4"}
    said = [e for e in m.take_events() if "cannot tell which one" in e]
    assert len(said) == 1 and "COM5, COM6" in said[0]


def test_usb_id_in_the_profile_finds_the_converter_at_start_whatever_its_number():
    sim = _bus(1)
    world = {"present": {"COM4": sim}, "generation": 0, "closed": 0, "opens": []}

    def factory(port, baud, timeout):
        world["opens"].append(port)
        if port not in world["present"]:
            raise OSError("could not open port")
        return Port(world, port)

    ports = lambda: [dict(USB, device="COM4", description="USB Serial Port")]
    turbos = _turbos(1, port="COM3")                          # the profile still says COM3
    turbos[0].params["usb_id"] = "0403:6001"
    m = RS485Turbos(turbos, serial_factory=factory, port_lister=ports)
    m.open(start_threads=False)
    for b in m.buses.values():
        b.gap_s = 0.0
    m.poll_once_ordered()
    assert m.inputs("turbo1").comm_ok and m.inputs("turbo1").rs485["port"] == "COM4"
    assert any("is now COM4" in e for e in m.take_events())
    # with the serial number of ANOTHER converter in the profile it is left alone
    turbos = _turbos(1, port="COM3")
    turbos[0].params["usb_id"] = "0403:6001:ZZZZ"
    m = RS485Turbos(turbos, serial_factory=factory, port_lister=ports)
    m.open(start_threads=False)
    assert "COM3" in m.port_errors


# ---------------------------------------------------------------------------- the poller itself
def test_the_background_poller_survives_a_port_error_and_carries_on():
    """The failure as it happened: threads running, the port gone for a moment."""
    m, world, sim, _ = rig(2, start_threads=True, lister=False)
    try:
        m.set_commands({"turbo1": True, "turbo2": True}, {})
        time.sleep(0.4)
        assert sim.pumps[1].p[10] and m.inputs("turbo1").comm_ok
        unplug(world)
        time.sleep(0.5)
        assert all(th.is_alive() for th in m._threads), "a port error must not end the poller"
        world["present"]["COM4"] = sim
        deadline = time.time() + 6.0
        while time.time() < deadline and "COM4" in m.port_errors:
            time.sleep(0.05)
        time.sleep(0.3)
        assert "COM4" not in m.port_errors and all(th.is_alive() for th in m._threads)
        assert m.clock() - m.units["turbo1"].last_ok < 0.5, "the pumps must be read again"
    finally:
        m._stop.set()


def test_an_unexpected_error_in_a_pass_is_logged_and_the_poller_carries_on():
    m, world, sim, _ = rig(1, start_threads=True, lister=False)
    try:
        real, calls = m._poll, {"n": 0}

        def broken(u):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise RuntimeError("something nobody thought of")
            return real(u)
        m._poll = broken
        time.sleep(0.6)
        assert all(th.is_alive() for th in m._threads)
        said = [e for e in m.take_events() if "unexpected RuntimeError" in e]
        assert len(said) == 1, said                          # said once, not on every pass
        assert m.clock() - m.units["turbo1"].last_ok < 0.5
    finally:
        m._stop.set()


def test_a_poller_that_is_not_running_is_started_again_from_the_controller_loop():
    m, world, sim, _ = rig(1, start_threads=True, lister=False)
    try:
        time.sleep(0.2)
        m._stop.set()                                        # end the thread behind the manager's back
        for th in m._threads:
            th.join(2.0)
        assert not any(th.is_alive() for th in m._threads)
        m._stop.clear()
        events = m.take_events()                             # what the controller calls on every pass
        assert any("started again" in e for e in events) and all(th.is_alive() for th in m._threads)
        time.sleep(0.3)
        assert m.clock() - m.units["turbo1"].last_ok < 0.5
    finally:
        m._stop.set()


# ---------------------------------------------------------------------------- the log
def test_pumps_that_are_switched_off_do_not_fill_the_log():
    """Port fine, pump electronics off (overnight): the port is still reset every `port_reset_s`,
    as before - but that is one line, then one every ten minutes."""
    m, world, sim, t = rig(4, port_reset_s=10.0, timeout_s=0.001)
    m.poll_once_ordered()
    m.take_events()
    sim.dead = True
    resets0 = len(world["opens"])
    lines = []
    for _ in range(720):                                      # one hour, a pass every 5 s
        t[0] += 5.0
        m.poll_once_ordered()
        lines += m.take_events()
    assert 300 <= len(world["opens"]) - resets0 <= 361         # the resets themselves go on
    assert len(lines) <= 8, lines
    assert "re-opened" in lines[0] and "more like this" in lines[-1]
    sim.dead = False
    t[0] += 1.0
    m.poll_once_ordered()
    m.poll_once_ordered()
    assert any("answering again" in e for e in m.take_events())
    assert m.inputs("turbo3").comm_ok
