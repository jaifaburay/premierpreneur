"""
The public functions of the ingest package.

    prepare(path)                  read and clean one file; write nothing; describe the result
    commit(prepared, db_path)      save a prepared result as one SQLite dataset file
    ingest_file(path, db_path)     prepare() then commit()
    ingest_folder(folder, db_path) the same for several files that together form one dataset
    set_assumptions, summarize     live in store.py

The split into prepare and commit exists so the app can show the owner what was understood
(which column is the quantity, which is the price) and let them correct it before anything
is saved.

prepare() never raises because of a bad file. Whatever is wrong with the upload comes back
as {"status": "invalid", "reason_code": ..., "reason": "<one sentence>"}.
"""
from __future__ import annotations

import copy
from pathlib import Path

from .canonical import build_canonical
from .cleaning import clean_sheet
from .readers import SUPPORTED_EXTENSIONS, InvalidFile, open_file
from .roles import apply_overrides, build_mapping, detect_roles, role_named_by, role_words, table_kind
from .store import read_report, summarize, validate_assumptions, write_database
from .textclean import display_file_name, normalise_header, table_name_for

DEFAULT_MAX_BYTES = 50_000_000
DEFAULT_MAX_ROWS = 2_000_000
LOW_CONFIDENCE = 0.7           # roles guessed with less confidence than this get a warning


# --------------------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------------------
def prepare(path, original_name=None, max_bytes: int = DEFAULT_MAX_BYTES, max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Read and clean one uploaded file. Nothing is written.

    path           where the upload is stored
    original_name  the name the file had on the owner's computer (uploads are often stored
                   under a temporary name); used for the file type and for display only
    Returns the dictionary described in the build spec, section 5. "status" is "ok" or
    "invalid"; an invalid result carries a reason code and one sentence for the owner.
    """
    return prepare_files([path], [original_name], max_bytes=max_bytes, max_rows=max_rows)


def prepare_files(paths, original_names=None, max_bytes: int = DEFAULT_MAX_BYTES,
                  max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Like prepare(), for several files that together make one dataset (for example
    products.csv, sales.csv, inventory.csv and suppliers.csv). The size limit applies to
    each file, the row limit to all of them together. A file that cannot be used is skipped
    with a warning as long as at least one other file is usable."""
    try:
        return _prepare_files(list(paths), original_names, max_bytes, max_rows)
    except InvalidFile as problem:
        return _invalid(problem.code, problem.reason)
    except Exception as error:
        # A safety net: no bad file may crash the caller. The message names the kind of
        # error only, never file content or a path.
        return _invalid("unreadable", f"The file could not be read ({type(error).__name__}). "
                                      "Open it in Excel and save it again as .xlsx or .csv.")


def _invalid(code: str, reason: str, report=None, warnings=None) -> dict:
    return {"status": "invalid", "reason_code": code, "reason": reason, "tables": [],
            "report": report or [], "mapping": {}, "warnings": warnings or [], "_frames": {}}


def _prepare_files(paths: list, original_names, max_bytes: int, max_rows: int) -> dict:
    names = list(original_names) if original_names else [None] * len(paths)
    if not paths:
        return _invalid("empty_file", "No file was given.")
    clean_tables, report, warnings, problems = [], [], [], []
    taken_names = set()
    rows_left = max_rows
    for path, name in zip(paths, names):
        display = display_file_name(name or Path(path).name)
        try:
            tables, rows_read = _read_one_file(Path(path), display, max_bytes, rows_left, taken_names, report, warnings)
        except InvalidFile as problem:
            if problem.code == "too_many_rows":
                problem = InvalidFile("too_many_rows",
                                      f"The data has more than {max_rows:,} rows, which is the limit. "
                                      "Upload a shorter period or fewer sheets.")
            problems.append((display, problem))
            continue
        rows_left -= rows_read
        clean_tables += tables

    if not clean_tables:
        _display, first = problems[0]
        return _invalid(first.code, first.reason, report, warnings)
    for display, problem in problems:
        report.append(_row("file_skipped", "", 1, f"File '{display}' was not used: {problem.reason}"))
        warnings.append(f"File '{display}' was not used: {problem.reason}")

    tables, frames = [], {}
    for table in clean_tables:
        unusable = [(name, role, f"{why}, so it was not used")
                    for name, role, why in detect_roles(table.columns, table.frame)]
        unusable += [(normalise_header(header) or "(unnamed)", role_named_by(header), "it has no values, so it was dropped")
                     for header in table.empty_headers if role_named_by(header)]
        # A column that names a role but cannot be used is reported under the step
        # "unusable_<role>". The capability reasons read these rows, so that the owner is
        # told "the stock column is empty" instead of "add a stock column".
        for name, role, why in unusable:
            sentence = f"Column {name} in {table.name} is named like {role_words(role)}, but {why}."
            report.append(_row(f"unusable_{role}", table.name, 1, sentence))
            warnings.append(sentence)
        tables.append({"name": table.name, "source_file": table.source_file, "source_sheet": table.source_sheet,
                       "rows": len(table.frame), "kind": table_kind(table.columns), "columns": table.columns})
        frames[table.name] = table.frame
    warnings += _mapping_warnings(tables)
    return {"status": "ok", "reason_code": None, "reason": None, "tables": tables, "report": report,
            "mapping": build_mapping(tables), "warnings": warnings, "_frames": frames}


def _row(step: str, table: str, count: int, detail: str) -> dict:
    return {"step": step, "table": table, "count": int(count), "detail": detail}


def _read_one_file(path: Path, display: str, max_bytes: int, rows_left: int, taken_names: set,
                   report: list, warnings: list) -> tuple:
    """Open one file and clean each of its sheets. Returns (clean tables, rows read).
    Raises InvalidFile when the file has no usable table."""
    sheets, notes = open_file(path, display, max_bytes)
    report += [_row(step, "", count, detail) for step, count, detail in notes]
    warnings += [detail for step, _count, detail in notes if step == "file_type_mismatch"]

    tables, problems, rows_read = [], [], 0
    for sheet in sheets:
        # A workbook's tables are named after its sheets; a text file's single "sheet" is
        # named after the file (see readers._open_text).
        table_name = table_name_for(sheet.name, taken_names)
        result = clean_sheet(sheet, table_name, display, rows_left - rows_read)
        rows_read += result.rows_read
        if result.table is None:
            taken_names.discard(table_name)
            problems.append(result.problem)
            report.append(_row("sheet_skipped", "", 1, f"Sheet '{result.sheet_name}': {_PROBLEM_TEXT[result.problem]}"))
            continue
        report += [_row(step, table_name, count, detail) for step, count, detail in result.steps]
        warnings += result.warnings
        tables.append(result.table)

    if tables:
        return tables, rows_read
    if all(problem == "blank" for problem in problems):
        raise InvalidFile("empty_file", "The file has no data: every sheet is blank.")
    if "header_only" in problems:
        raise InvalidFile("no_data", "The file has column headers but no data rows under them.")
    raise InvalidFile("no_usable_table",
                      "No table was found in the file. A usable table has a row of column headers "
                      "and at least two columns of data under it.")


_PROBLEM_TEXT = {
    "blank": "the sheet is blank.",
    "header_only": "there are headers but no data rows.",
    "one_column": "fewer than two columns of data were found.",
}


def _mapping_warnings(tables: list) -> list:
    """Sentences for the owner about what could not be recognised."""
    warnings = []
    if not any(table["kind"] == "transactions" for table in tables):
        warnings.append("No sales table was recognised. A sales table needs a product column, a date "
                        "column and a quantity column; without one only general questions can be answered.")
    for table in tables:
        for column in table["columns"]:
            if column["role"] and column["confidence"] < LOW_CONFIDENCE:
                warnings.append(f"Column {column['name']} in {table['name']} was guessed to be "
                                f"{column['role']} (confidence {column['confidence']}); please check it.")
    return warnings


# --------------------------------------------------------------------------------------
# commit
# --------------------------------------------------------------------------------------
def commit(prepared: dict, db_path, mapping=None, assumptions=None) -> dict:
    """Save a prepared dataset as one SQLite file at db_path, replacing any file there.

    mapping      the owner's corrections to the roles:
                 {"quantity": {"table": "t_sales", "column": "units"}}  use this column
                 {"unit_cost": None}                                    the data has no such column
                 Roles not mentioned keep what prepare() found (prepared["tables"]).
    assumptions  owner defaults, e.g. {"default_lead_time_days": 14, "default_moq": 1}

    Returns summarize(db_path). Raises ValueError when `prepared` is not an "ok" result or
    when a correction or an assumption is not valid; nothing is written in that case.
    """
    if not isinstance(prepared, dict) or prepared.get("status") != "ok":
        raise ValueError("There is nothing to save: the file was not read successfully.")
    frames = prepared["_frames"]
    tables = copy.deepcopy(prepared["tables"])           # leave the caller's copy untouched
    apply_overrides(tables, frames, mapping)
    checked = validate_assumptions(assumptions or {})
    defaults = {name: value for name, value in checked.items() if value is not None}

    canonical, steps = build_canonical(tables, frames)
    report = list(prepared["report"]) + [_row(step, table, count, detail) for step, table, count, detail in steps]
    write_database(db_path, tables, frames, canonical, report, defaults)
    return summarize(db_path)


# --------------------------------------------------------------------------------------
# One-call helpers
# --------------------------------------------------------------------------------------
def ingest_file(path, db_path, original_name=None, mapping=None, assumptions=None,
                max_bytes: int = DEFAULT_MAX_BYTES, max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """prepare() then commit(). Returns
    {"status", "reason_code", "reason", "report", "summary", "warnings"}; for an invalid
    file "summary" is None and no database is written.

    max_bytes and max_rows are the limits of prepare(). Raise them to load a file that is
    larger than an upload may be, for example the million-row test file (about 76 MB)."""
    prepared = prepare(path, original_name, max_bytes=max_bytes, max_rows=max_rows)
    return _finish(prepared, db_path, mapping, assumptions)


def ingest_files(paths, db_path, original_names=None, mapping=None, assumptions=None,
                 max_bytes: int = DEFAULT_MAX_BYTES, max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Several files into one dataset; see prepare_files() for the limits."""
    prepared = prepare_files(paths, original_names, max_bytes=max_bytes, max_rows=max_rows)
    return _finish(prepared, db_path, mapping, assumptions)


def ingest_folder(folder, db_path, mapping=None, assumptions=None,
                  max_bytes: int = DEFAULT_MAX_BYTES, max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Every .csv, .tsv, .txt, .xlsx, .xlsm and .xls file in a folder (not its sub-folders),
    in name order, into one dataset. This is how the built-in store in data/ is loaded:
    products.csv, sales.csv, inventory.csv and suppliers.csv become one database."""
    folder = Path(folder)
    if not folder.is_dir():
        return {"status": "invalid", "reason_code": "unreadable", "reason": "That folder does not exist.",
                "report": [], "summary": None, "warnings": []}
    paths = sorted(path for path in folder.iterdir()
                   if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS)
    return ingest_files(paths, db_path, mapping=mapping, assumptions=assumptions,
                        max_bytes=max_bytes, max_rows=max_rows)


def _finish(prepared: dict, db_path, mapping, assumptions) -> dict:
    if prepared["status"] != "ok":
        return {"status": "invalid", "reason_code": prepared["reason_code"], "reason": prepared["reason"],
                "report": prepared["report"], "summary": None, "warnings": prepared["warnings"]}
    summary = commit(prepared, db_path, mapping=mapping, assumptions=assumptions)
    return {"status": "ok", "reason_code": None, "reason": None, "report": read_report(db_path),
            "summary": summary, "warnings": prepared["warnings"]}
