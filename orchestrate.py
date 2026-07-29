#!/usr/bin/env python3

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STEPS = (
    ("Build bundles", "make_bundles.py"),
    ("Publish to R2", "push.py"),
)


def main() -> int:
    for label, script in STEPS:
        print(f"\n==== {label} ({script}) ====", flush=True)
        result = subprocess.run([sys.executable, str(ROOT / script)], cwd=ROOT)
        if result.returncode != 0:
            print(
                f"\nERROR: {script} exited with code {result.returncode}; "
                "stopping orchestrator.",
                file=sys.stderr,
            )
            return result.returncode

    print("\n==== Orchestration complete ====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
