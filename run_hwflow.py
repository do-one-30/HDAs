#!/usr/bin/env python3
"""Run hwflow without installing it or setting PYTHONPATH:

  python3 /path/to/hwflow_pkg/run_hwflow.py all /path/to/design/build.yaml
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hwflow.__main__ import main  # noqa: E402

sys.exit(main())
