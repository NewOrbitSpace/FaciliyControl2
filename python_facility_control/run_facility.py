#!/usr/bin/env python3
"""Launch the facility control GUI.  `python run_facility.py --help` for options."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from facility_control.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
