#!/usr/bin/env python3
"""Run preparation, cached routing, validation, optimization, and output checks."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = {
    "prepare": PROJECT_ROOT / "calc" / "prepare_optimization_inputs.py",
    "routes": PROJECT_ROOT / "calc" / "build_route_matrix.py",
    "validate_inputs": PROJECT_ROOT / "calc" / "validate_optimization_inputs.py",
    "optimize": PROJECT_ROOT / "optim" / "optimize_allocations.py",
    "validate_outputs": PROJECT_ROOT / "optim" / "validate_optimization_outputs.py",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-routes", action="store_true",
        help="Discard logical cache reuse and request every OSRM matrix entry again.",
    )
    return parser.parse_args()


def run(step: str, *args: str) -> None:
    script = SCRIPTS[step]
    print(f"\n== {script.relative_to(PROJECT_ROOT)} {' '.join(args)} ==", flush=True)
    subprocess.run([sys.executable, str(script), *args], cwd=PROJECT_ROOT, check=True)


def main() -> None:
    args = arguments()
    run("prepare")
    route_args = ("--refresh",) if args.refresh_routes else ()
    run("routes", *route_args)
    run("validate_inputs")
    run("optimize")
    run("validate_outputs")


if __name__ == "__main__":
    main()
