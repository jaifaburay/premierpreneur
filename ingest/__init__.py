"""
ingest: turn any uploaded Excel or CSV file into a clean SQLite dataset.

This package is the only door through which an uploaded file enters PremierPreneur. It uses
no AI model at any point: every step is ordinary Python, so the same file always gives the
same result and text inside a file can never steer the cleaning.

What happens to an upload, and where the code is:

    readers.py    What kind of file is it really? Hand out its raw rows.
    textclean.py  Clean each piece of text; replace instruction-like text and links.
    values.py     Read single values: numbers, yes/no, dates.
    cleaning.py   Find the header, drop junk rows, decide and convert column types.
    roles.py      Work out what each column means (product, date, quantity, price, ...).
    canonical.py  Fill the four standard tables the tools read.
    store.py      Write the SQLite file; capabilities, assumptions, summary.
    api.py        The functions below.

Typical use:

    from ingest import prepare, commit
    prepared = prepare("upload.xlsx")
    if prepared["status"] == "invalid":
        print(prepared["reason"])
    else:
        summary = commit(prepared, "dataset.db")

display_file_name(name) gives the cleaned name under which a file is stored and shown
(no folders, instruction-like text replaced); it is the "source_file" of every table.

Command line:  python -m ingest <file or folder> [--db out.db]
"""
from .api import commit, ingest_file, ingest_files, ingest_folder, prepare, prepare_files
from .store import read_report, set_assumptions, summarize
from .textclean import display_file_name

__all__ = [
    "prepare", "commit", "ingest_file", "set_assumptions", "summarize",
    "prepare_files", "ingest_files", "ingest_folder", "read_report", "display_file_name",
]
