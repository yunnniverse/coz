#!/usr/bin/env python3

from __future__ import annotations

import os
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_ROOT = Path(os.getenv("MCOZ_SOCIAL_RESULTS_ROOT", SCRIPT_DIR / "results"))


def default_results_dir(name: str) -> str:
    return str(RESULTS_ROOT / name)
