"""Lets the folder itself be run:  python3 <path to>/relabel-waybill-generator  [flags]"""

import runpy
import sys
from pathlib import Path

script = Path(__file__).resolve().parent / "capture_waybill.py"
sys.argv[0] = str(script)
runpy.run_path(str(script), run_name="__main__")
