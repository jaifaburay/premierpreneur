"""
Opening an uploaded file and handing out its raw rows.

This module answers two questions and nothing else:

  1. What kind of file is this really? The name says one thing, the first bytes say another;
     the bytes win. A text file renamed to .xlsx is read as text; a damaged or locked
     workbook is refused with a reason.
  2. What are the rows? Every reader returns RawSheet objects. A RawSheet can be walked from
     the top as many times as needed and yields plain lists whose cells are str or None.
     Numbers and dates from Excel are written out as text here, so the cleaning code has
     one single kind of input whatever the file type was.

Safety: files are only read. Macros are never run (openpyxl does not execute them and the
workbook is opened for values only), formulas are read as their saved results, external
links are not loaded, and a workbook that would unpack to an enormous size is refused
before it is opened.
"""
from __future__ import annotations

import codecs
import csv
import datetime
import io
import zipfile
from pathlib import Path

SUPPORTED_EXTENSIONS = (".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt")
EXCEL_EXTENSIONS = (".xlsx", ".xlsm", ".xls")

MAX_COLUMNS = 200                        # columns read from one sheet; the rest are ignored
MAX_SHEETS = 20                          # sheets read from one workbook
MAX_CELL_CHARACTERS = 32_767             # the longest cell Excel allows
REMEMBER_ROWS = 100_000                  # Excel sheets up to this many rows are kept in memory
_EMPTY_ROW = ()                          # one shared object for every blank Excel row
MAX_UNZIPPED_BYTES = 1_500_000_000       # a workbook is a zip file; refuse "zip bombs"
MAX_SHARED_STRINGS_BYTES = 250_000_000   # the one part of a workbook that is held in memory

_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"          # old .xls, and encrypted .xlsx
# Excel files from before 1997 have no container at all: they start with a "BOF" record.
_EARLY_XLS_MAGIC = (b"\x09\x00\x04\x00", b"\x09\x02\x06\x00", b"\x09\x04\x06\x00")
# An encrypted .xlsx is an old-style container holding a stream with this name (UTF-16).
_ENCRYPTED_MARKER = "EncryptedPackage".encode("utf-16-le")

_DELIMITERS = (",", ";", "\t", "|")


class InvalidFile(Exception):
    """The upload cannot be used. `code` is one of the reason codes of prepare();
    `reason` is one plain sentence for the owner."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


class RawSheet:
    """One sheet (or one text file) as raw rows.

    rows() starts again from the first row every time it is called. The cleaning code walks
    a sheet three times (to find the header, to look at every value, to convert), so a large
    file never has to be held in memory as text.

    Reading an Excel sheet is slow (the file is zipped XML), so an Excel sheet of up to
    REMEMBER_ROWS rows is read once and kept; longer sheets and all text files are read
    again from the file on every walk.
    """

    def __init__(self, name: str, row_source, remember: bool = False):
        self.name = name
        self.columns_cut = False             # set when a row had more than MAX_COLUMNS cells
        self._row_source = row_source        # a function (sheet) -> generator of rows
        self._remember = remember
        self._kept = None

    def rows(self):
        if self._remember and self._kept is None:
            self._try_to_keep()
        if self._kept is not None:
            return _replay(self._kept)
        return _guarded(self._row_source(self))

    def _try_to_keep(self) -> None:
        """Read the sheet into memory, giving up when it has too many filled rows."""
        kept, filled = [], 0
        rows = _guarded(self._row_source(self))
        try:
            for row in rows:
                kept.append(row)
                filled += 1 if row else 0
                if filled > REMEMBER_ROWS:
                    self._remember = False           # too long: read from the file every time
                    return
        finally:
            rows.close()
        self._kept = kept


def _replay(rows: list):
    """Rows kept in memory, as a generator like the file readers give."""
    for row in rows:
        yield row


def _guarded(rows):
    """Pass rows through, turning any reader error into InvalidFile so that a file that
    breaks half way down is reported as unreadable instead of crashing the caller."""
    try:
        for row in rows:
            yield row
    except InvalidFile:
        raise
    except Exception as error:               # the reader libraries raise many different types
        raise InvalidFile(
            "unreadable",
            f"The file could not be read to the end ({type(error).__name__}). "
            "Open it in Excel and save it again as .xlsx or .csv.",
        )
    finally:
        rows.close()                         # closes the file even when reading stops early


# --------------------------------------------------------------------------------------
# Deciding what the file is
# --------------------------------------------------------------------------------------
def open_file(path: Path, display_name: str, max_bytes: int) -> tuple:
    """Check the file and open it with the right reader.

    Returns (sheets, notes). `sheets` is a list of RawSheet. `notes` is a list of
    (step, count, detail) entries for the cleaning report, for example the text encoding
    that was used. Raises InvalidFile for anything that cannot be read.
    """
    extension = Path(display_name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise InvalidFile(
            "unsupported_type",
            f"Files ending in '{extension or '(nothing)'}' cannot be read. "
            "Upload an Excel workbook (.xlsx, .xlsm, .xls) or a text table (.csv, .tsv, .txt).",
        )
    if not path.is_file():
        raise InvalidFile("unreadable", "The uploaded file could not be found. Upload it again.")
    size = path.stat().st_size
    if size == 0:
        raise InvalidFile("empty_file", "The file is empty (0 bytes).")
    if size > max_bytes:
        raise InvalidFile(
            "too_large",
            f"The file is {size / 1_000_000:.1f} MB; the limit is {max_bytes / 1_000_000:.0f} MB. "
            "Split it into smaller files or remove columns you do not need.",
        )

    with path.open("rb") as handle:
        start = handle.read(8192)
    notes = []

    if start.startswith(_ZIP_MAGIC):
        sheets = _open_xlsx(path, notes)
        kind = "an Excel workbook"
        matches_name = extension in (".xlsx", ".xlsm")
    elif start.startswith(_OLE_MAGIC):
        if _file_contains(path, _ENCRYPTED_MARKER):
            raise InvalidFile(
                "password_protected",
                "The workbook is protected with a password. Remove the password in Excel "
                "(File > Info > Protect Workbook) and upload it again.",
            )
        sheets = _open_xls(path, notes)
        kind = "an old-style Excel workbook"
        matches_name = extension == ".xls"
    elif start.startswith(_EARLY_XLS_MAGIC):
        sheets = _open_xls(path, notes)
        kind = "an old-style Excel workbook"
        matches_name = extension == ".xls"
    else:
        sheets = _open_text(path, start, display_name, notes)
        kind = "a plain text table"
        matches_name = extension in (".csv", ".tsv", ".txt")

    if not matches_name:
        notes.append(("file_type_mismatch", 1,
                      f"The name ends in '{extension}' but the content is {kind}; it was read as {kind}."))
    return sheets, notes


def _file_contains(path: Path, marker: bytes) -> bool:
    """Search a binary file for a byte string, a block at a time."""
    overlap = len(marker) - 1
    previous_tail = b""
    with path.open("rb") as handle:
        while True:
            block = handle.read(1_000_000)
            if not block:
                return False
            if marker in previous_tail + block:
                return True
            previous_tail = block[-overlap:]


# --------------------------------------------------------------------------------------
# Text files: .csv, .tsv, .txt
# --------------------------------------------------------------------------------------
def _open_text(path: Path, start: bytes, display_name: str, notes: list) -> list:
    encoding = _detect_encoding(path, start)
    with path.open("r", encoding=encoding, newline="") as handle:
        sample = handle.read(65536)
    if not sample.strip():
        # Whitespace only at the top. Treat as empty unless there is real text further down.
        if not _has_text(path, encoding):
            raise InvalidFile("empty_file", "The file has no content, only blank space.")
    if sample.lstrip().startswith("<"):
        raise InvalidFile(
            "unreadable",
            "The file is a web page (HTML or XML), not a table. Open it in Excel and use "
            "Save As to store it as .xlsx or .csv.",
        )
    delimiter = _sniff_delimiter(sample)
    names = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
    notes.append(("text_format", 1, f"Read as text: encoding {encoding}, {names[delimiter]}-separated."))
    sheet_name = Path(display_name).stem

    unclosed_quote = InvalidFile(
        "unreadable",
        "The text file has a quotation mark that is never closed, so its rows cannot be "
        "separated. Open it in Excel and save it again as .csv.",
    )

    def row_source(sheet: RawSheet):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                for row in csv.reader(handle, delimiter=delimiter):
                    # A quote that is never closed swallows the rest of the file into one
                    # cell. No real cell is that long (Excel's own limit is 32,767).
                    if row and max(map(len, row)) > MAX_CELL_CHARACTERS:
                        raise unclosed_quote
                    if len(row) > MAX_COLUMNS:
                        sheet.columns_cut = True
                        row = row[:MAX_COLUMNS]
                    yield row
        except csv.Error:                    # the csv module's own limit on one cell
            raise unclosed_quote

    return [RawSheet(sheet_name, row_source)]


def _detect_encoding(path: Path, start: bytes) -> str:
    """Pick the first encoding that can decode the whole file.

    Order: UTF-16 and UTF-8 when the file announces them with a byte-order mark, then UTF-8,
    then Windows-1252 (what Excel on Windows writes), then Latin-1, which accepts any bytes.
    A file full of NUL or other control bytes is not text at all and is refused.
    """
    if start.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if start.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    # More than 1% control bytes (and more than a stray handful) means binary data.
    control_bytes = sum(1 for byte in start if byte < 9 or 13 < byte < 32)
    if control_bytes > max(8, len(start) // 100):
        raise InvalidFile(
            "unreadable",
            "The file is not a readable spreadsheet or text table (it holds binary data). "
            "It may be damaged; export it again from the program it came from.",
        )
    for encoding in ("utf-8", "cp1252"):
        if _decodes_fully(path, encoding):
            return encoding
    return "latin-1"


def _decodes_fully(path: Path, encoding: str) -> bool:
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1_000_000)
                if not block:
                    decoder.decode(b"", final=True)
                    return True
                decoder.decode(block)
    except UnicodeDecodeError:
        return False


def _has_text(path: Path, encoding: str) -> bool:
    with path.open("r", encoding=encoding, newline="") as handle:
        for line in handle:
            if line.strip():
                return True
    return False


def _sniff_delimiter(sample: str) -> str:
    """Choose the separator: comma, semicolon, tab or pipe.

    Rule: split the first lines with each candidate and count the fields per line. The best
    candidate is the one for which the most lines agree on one field count of two or more;
    when two candidates tie, the one that gives more fields wins (so "1,5;2,5;3,5" is read
    as three semicolon-separated fields, not two comma-separated ones). If no candidate
    splits the lines, comma is returned and the file is read as a single column.
    """
    best = (0.0, 0, ",")
    for delimiter in _DELIMITERS:
        counts = []
        try:
            for row in csv.reader(io.StringIO(sample), delimiter=delimiter):
                if any(cell.strip() for cell in row):
                    counts.append(len(row))
                if len(counts) >= 200:
                    break
        except csv.Error:
            continue
        if len(counts) > 1 and len(sample) >= 65536:
            counts = counts[:-1]                  # the sample may end in the middle of a line
        if not counts:
            continue
        common = max(set(counts), key=counts.count)
        if common < 2:
            continue
        share = counts.count(common) / len(counts)
        if (share, common) > best[:2]:
            best = (share, common, delimiter)
    return best[2]


# --------------------------------------------------------------------------------------
# Excel workbooks: .xlsx and .xlsm
# --------------------------------------------------------------------------------------
def _open_xlsx(path: Path, notes: list) -> list:
    try:
        with zipfile.ZipFile(path) as archive:
            parts = {info.filename: info.file_size for info in archive.infolist()}
    except (zipfile.BadZipFile, OSError, ValueError):
        raise InvalidFile(
            "unreadable",
            "The workbook is damaged and cannot be opened. Open it in Excel, let Excel repair "
            "it, and save it again.",
        )
    if "xl/workbook.xml" not in parts:
        if "xl/workbook.bin" in parts:
            raise InvalidFile(
                "unsupported_type",
                "This is a binary Excel workbook (.xlsb). Open it in Excel and use Save As > "
                "Excel Workbook (.xlsx).",
            )
        raise InvalidFile(
            "unreadable",
            "The file is a zip archive but not an Excel workbook. Upload the spreadsheet itself.",
        )
    if (sum(parts.values()) > MAX_UNZIPPED_BYTES
            or parts.get("xl/sharedStrings.xml", 0) > MAX_SHARED_STRINGS_BYTES):
        raise InvalidFile(
            "too_large",
            "The workbook unpacks to more data than can be handled. Save the sheet you need "
            "as a .csv file and upload that.",
        )

    try:
        import openpyxl
    except ImportError:
        raise InvalidFile("unsupported_type",
                          "Excel files need the openpyxl package. Run: pip install openpyxl")
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
        titles = [sheet.title for sheet in workbook.worksheets]
        workbook.close()
    except Exception as error:
        raise InvalidFile(
            "unreadable",
            f"The workbook could not be opened ({type(error).__name__}). Open it in Excel and "
            "save it again as .xlsx.",
        )

    titles = _first_sheets(titles, notes)

    def make_source(index: int):
        def row_source(sheet: RawSheet):
            # read_only streams the rows instead of loading the sheet; data_only gives the
            # saved result of each formula instead of the formula text.
            book = openpyxl.load_workbook(path, read_only=True, data_only=True, keep_links=False)
            try:
                worksheet = book.worksheets[index]
                worksheet.reset_dimensions()     # do not trust the size the file claims
                for row in worksheet.iter_rows(values_only=True):
                    if len(row) > MAX_COLUMNS:
                        sheet.columns_cut = sheet.columns_cut or any(value is not None for value in row[MAX_COLUMNS:])
                        row = row[:MAX_COLUMNS]
                    cells = [value_to_text(value) for value in row]
                    yield cells if any(cell is not None for cell in cells) else _EMPTY_ROW
            finally:
                book.close()
        return row_source

    return [RawSheet(str(title), make_source(index), remember=True) for index, title in enumerate(titles)]


def _first_sheets(titles: list, notes: list) -> list:
    """Keep at most MAX_SHEETS sheets and say so in the report when some are left out."""
    if len(titles) > MAX_SHEETS:
        notes.append(("sheets_ignored", len(titles) - MAX_SHEETS,
                      f"The workbook has {len(titles)} sheets; only the first {MAX_SHEETS} were read."))
    return titles[:MAX_SHEETS]


# --------------------------------------------------------------------------------------
# Old Excel workbooks: .xls (needs the optional xlrd package)
# --------------------------------------------------------------------------------------
def _open_xls(path: Path, notes: list) -> list:
    try:
        import xlrd
    except ImportError:
        raise InvalidFile(
            "unsupported_type",
            "This is an old-style .xls workbook. Open it in Excel and use Save As > Excel "
            "Workbook (.xlsx), then upload the .xlsx file.",
        )
    try:
        # use_mmap=False: read the file into memory and close it at once. (A memory-mapped
        # file stays locked on Windows, and the upload could not be deleted afterwards.)
        book = xlrd.open_workbook(str(path), logfile=io.StringIO(), on_demand=True, use_mmap=False)
        titles = book.sheet_names()
    except Exception as error:
        if "encrypt" in str(error).lower() or "password" in str(error).lower():
            raise InvalidFile(
                "password_protected",
                "The workbook is protected with a password. Remove the password in Excel and "
                "upload it again.",
            )
        raise InvalidFile(
            "unreadable",
            f"The .xls workbook could not be opened ({type(error).__name__}). Open it in Excel "
            "and save it again as .xlsx.",
        )

    titles = _first_sheets(titles, notes)

    def make_source(index: int):
        def row_source(sheet: RawSheet):
            worksheet = book.sheet_by_index(index)
            sheet.columns_cut = sheet.columns_cut or worksheet.ncols > MAX_COLUMNS
            for row_number in range(worksheet.nrows):
                cells = worksheet.row(row_number)[:MAX_COLUMNS]
                yield [_xls_cell_to_text(cell, book.datemode, xlrd) for cell in cells]
        return row_source

    return [RawSheet(str(title), make_source(index)) for index, title in enumerate(titles)]


def _xls_cell_to_text(cell, datemode: int, xlrd):
    if cell.ctype == xlrd.XL_CELL_DATE:
        return value_to_text(xlrd.xldate_as_datetime(cell.value, datemode))
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if cell.value else "FALSE"
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
        return None
    return value_to_text(cell.value)


# --------------------------------------------------------------------------------------
# Excel values as text
# --------------------------------------------------------------------------------------
def value_to_text(value):
    """Write one Excel cell value as text, the way it would look in a clean CSV file.

    5.0 -> "5", 2.5 -> "2.5", a date -> "2024-01-05", a date with a time ->
    "2024-01-05 13:45:00", True -> "TRUE". The text is typed again later by the same rules
    that are used for CSV files.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):                          # must come before int: True is an int
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    if isinstance(value, datetime.datetime):
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat(timespec="seconds")
    return str(value)
