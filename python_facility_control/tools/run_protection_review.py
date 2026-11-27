"""Offline-only test runner. Never launches the application or hardware drivers."""
import importlib.abc
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / '.review-deps'))
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
os.environ.setdefault('SAFETY_LEVEL', 'quick')

class NoHardware(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'nidaqmx' or fullname.startswith('nidaqmx.'):
            raise RuntimeError('Real NI driver imports prohibited in protection review')

sys.meta_path.insert(0, NoHardware())
import serial
def forbidden(*args, **kwargs):
    raise RuntimeError('Physical serial access prohibited in protection review')
serial.Serial = forbidden
serial.serial_for_url = forbidden
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
