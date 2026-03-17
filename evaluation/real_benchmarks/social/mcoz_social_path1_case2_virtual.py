#!/usr/bin/env python3

from __future__ import annotations

import sys

from mcoz_social_path1_single_case import main as single_main


if __name__ == "__main__":
    raise SystemExit(single_main(["--case-id", "case2", *sys.argv[1:]]))
