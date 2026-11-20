"""
Run every test script of the project, one after the other, and say which passed.

    python tests/run_all.py            every tests/test_*.py script, then a smoke test of the cleaning step
    python tests/run_all.py --quick    the same, but scripts that know --quick skip their big timing runs

Each test script is a plain program that prints one line per check and ends with
"N passed, M failed". This file starts each one as its own Python process (with the
same Python that runs this file), reads that last line and prints one line per script:
its pass and fail counts, how long it took, and PASS or FAIL. For a script that failed,
its FAIL lines are repeated at the end, so the cause is visible without running it again.

The smoke test is "python -m ingest data": the cleaning step run on the sample store's
folder, as a person would run it from the command line.

The exit code is 0 only when every script passed. No model server is needed for any of
it; nothing here measures the model.
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
TIME_LIMIT_SECONDS = 3600                                   # a script that runs longer than this is stopped
COUNTS = re.compile(r"(\d+) passed, (\d+) failed")          # the last line every test script prints


def run_script(arguments):
    """Start one Python program and wait for it. Returns (exit code, everything it printed, seconds)."""
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")       # the same output on Windows and Linux
    started = time.time()
    try:
        done = subprocess.run([sys.executable] + arguments, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=environment, timeout=TIME_LIMIT_SECONDS)
        code, printed = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        code, printed = 1, f"stopped: still running after {TIME_LIMIT_SECONDS} seconds"
    return code, printed, time.time() - started


def test_scripts():
    """Every tests/test_*.py file, in name order."""
    return sorted(TESTS.glob("test_*.py"))


def main():
    quick = "--quick" in sys.argv
    rows, details = [], []
    for script in test_scripts():
        arguments = [str(script)]
        if quick and "--quick" in script.read_text(encoding="utf-8"):
            arguments.append("--quick")
        code, printed, seconds = run_script(arguments)
        found = COUNTS.findall(printed)
        passed, failed = (int(found[-1][0]), int(found[-1][1])) if found else (0, 0)
        ok = code == 0 and failed == 0 and bool(found)
        rows.append((f"tests/{script.name}" + (" --quick" if len(arguments) > 1 else ""), passed, failed, seconds, ok))
        if not ok:
            lines = [line for line in printed.splitlines() if line.startswith("FAIL")]
            if not found:                                   # it crashed before its last line: show how it ended
                lines += printed.strip().splitlines()[-8:]
            details.append((script.name, lines))

    code, printed, seconds = run_script(["-m", "ingest", str(ROOT / "data")])
    ok = code == 0 and "Standard tables:" in printed
    rows.append(("python -m ingest data (smoke test)", 1 if ok else 0, 0 if ok else 1, seconds, ok))
    if not ok:
        details.append(("python -m ingest data", printed.strip().splitlines()[-8:]))

    print(f"{'script':40s} {'passed':>7s} {'failed':>7s} {'seconds':>8s}  result")
    for name, passed, failed, seconds, ok in rows:
        print(f"{name:40s} {passed:7d} {failed:7d} {seconds:8.1f}  {'PASS' if ok else 'FAIL'}")
    failing = [name for name, _, _, _, ok in rows if not ok]
    total_passed = sum(row[1] for row in rows)
    total_failed = sum(row[2] for row in rows)
    print(f"{'total':40s} {total_passed:7d} {total_failed:7d} {sum(row[3] for row in rows):8.1f}  "
          f"{'PASS' if not failing else 'FAIL'}")
    for name, lines in details:
        print(f"\n--- what failed in {name} ---")
        for line in lines:
            print(line)
    if failing:
        print(f"\n{len(failing)} of {len(rows)} failed: {', '.join(failing)}")
    else:
        print(f"\nall {len(rows)} passed")
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
