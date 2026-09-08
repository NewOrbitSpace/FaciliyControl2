import math

import pytest

from facility_control import gauges, units


def test_convectron_formula_matches_vi():
    # VI: P_Conve_Torr = 10**V * 1E-4
    assert gauges.convectron_v_to_torr(0.0) == pytest.approx(1e-4)
    assert gauges.convectron_v_to_torr(6.88) == pytest.approx(10**6.88 * 1e-4)
    for p in (1e-4, 1e-2, 1.0, 760.0):
        assert gauges.convectron_v_to_torr(gauges.convectron_torr_to_v(p)) == pytest.approx(p, rel=1e-9)


def test_ion_gauge_formula_matches_vi():
    # VI: P_WRG_Torr = 10**(1.667*V-11.46)
    assert gauges.ion_gauge_v_to_torr(0.0) == pytest.approx(10 ** -11.46)
    assert gauges.ion_gauge_v_to_torr(6.0) == pytest.approx(10 ** (1.667 * 6 - 11.46))
    for p in (1e-9, 1e-5, 1e-2, 760.0):
        assert gauges.ion_gauge_v_to_torr(gauges.ion_gauge_torr_to_v(p)) == pytest.approx(p, rel=1e-9)


def test_speed_conversions():
    assert gauges.speed_pct_from_v(9.5) == pytest.approx(95.0)
    assert gauges.speed_pct_from_rpm(820 / 0.016667) == pytest.approx(100.0, rel=1e-3)


def test_units_roundtrip_and_format():
    assert units.torr_to("mbar", 750.0616827) == pytest.approx(1000.0, rel=1e-6)
    assert units.to_torr("mbar", units.torr_to("mbar", 0.25)) == pytest.approx(0.25)
    assert units.format_pressure(628.0, "torr") == "6.28E+2"
    assert units.format_pressure(1e-5, "torr") == "1.00E-5"
    assert units.format_pressure(float("nan"), "torr") == "NaN"
    assert units.unit_label("mbar") == "mBar"
