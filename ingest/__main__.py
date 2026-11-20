"""
Command line:  python -m ingest <file or folder> [--db out.db]

Prints the cleaning report, the tables with the role of each column, and the summary
(standard table counts, date range, capabilities). With --db the dataset is saved to that
file; without it nothing is kept. The exit code is 0 for a usable file and 1 for an invalid one.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from . import commit, prepare, prepare_files, read_report
from .readers import SUPPORTED_EXTENSIONS


def _print_report(report: list) -> None:
    print("Cleaning report")
    for row in report:
        table = row["table"] or "(file)"
        print(f"  {row['step']:28s} {table:24s} {row['count']:>9,}  {row['detail']}")


def _print_tables(tables: list) -> None:
    print("Tables")
    for table in tables:
        print(f"  {table['name']}  ({table['rows']:,} rows, kind: {table['kind']})")
        for column in table["columns"]:
            role = column.get("role") or "-"
            print(f"      {column['name']:32s} {column['dtype']:8s} role: {role}")


def _print_summary(summary: dict) -> None:
    counts = summary["canonical_counts"]
    print("Standard tables: " + ", ".join(f"{name} {count:,}" for name, count in counts.items()))
    dates = summary["date_range"]
    print(f"Sales from {dates['first']} to {dates['last']}")
    print("Capabilities")
    for name, state in summary["capabilities"].items():
        mark = "yes" if state["available"] else "no "
        print(f"  {mark}  {name:20s} {state['reason']}")
    if summary["assumptions"]:
        print(f"Assumptions: {summary['assumptions']}")


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest", description="Clean an Excel or CSV file.")
    parser.add_argument("path", help="the file to read, or a folder of files that form one dataset")
    parser.add_argument("--db", help="save the cleaned dataset to this SQLite file")
    options = parser.parse_args(arguments)
    # A Windows console cannot print every character a file may contain; replace the ones
    # it cannot show instead of stopping with an error.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    source = Path(options.path)
    if source.is_dir():
        paths = sorted(path for path in source.iterdir()
                       if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS)
        prepared = prepare_files(paths)
    else:
        prepared = prepare(source)

    if prepared["status"] != "ok":
        print(f"INVALID ({prepared['reason_code']}): {prepared['reason']}")
        return 1
    for warning in prepared["warnings"]:
        print(f"Warning: {warning}")
    if options.db:
        _save_and_print(prepared, options.db)
        print(f"Saved to {options.db}")
    else:
        # Nothing is to be kept: build the dataset in a temporary folder to be able to
        # show the same report and summary, then throw it away.
        with tempfile.TemporaryDirectory() as folder:
            _save_and_print(prepared, Path(folder) / "preview.db")
        print("Nothing was saved (give --db <file> to keep the dataset).")
    return 0


def _save_and_print(prepared: dict, db_path) -> None:
    summary = commit(prepared, db_path)
    _print_report(read_report(db_path))
    _print_tables(summary["tables"])
    _print_summary(summary)


if __name__ == "__main__":
    sys.exit(main())
