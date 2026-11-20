"""
Turning one raw sheet into one clean table.

The work is done in three steps. The sheet is read again from the top for each step, so
that a file of a million rows never has to sit in memory as text:

  1. HEAD.    The first 200 non-blank rows are read to find the header row.
  2. PASS 1.  Every row is read, cleaned and COUNTED: how many values of each column are
              numbers, dates, yes/no. Nothing is kept. At the end each column gets a type.
  3. PASS 2.  Every row is read and cleaned again, and each column is CONVERTED to its type.
              The converted chunks are kept and joined into one pandas DataFrame.

Rows are handled 50,000 at a time. Inside a chunk a column is a numpy array of str / None.

The judgment calls (how the header is found, when a column counts as a number column, what
a totals row is) are written next to the code that makes them and tested in
tests/test_ingest.py.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from itertools import zip_longest

import numpy as np
import pandas as pd

from .readers import MAX_COLUMNS, InvalidFile, RawSheet
from .textclean import (REMOVED_INSTRUCTION, REMOVED_LINK, clean_cell, clean_label,
                        normalise_header, unique_names)
from .values import (AMBIGUOUS_PAIRS, DATE_FORMATS, EXCEL_SERIAL_RANGE, YYYYMMDD_RANGE,
                     dates_to_iso, distinct_values, excel_serial_to_iso, looks_like_date,
                     number_shape, parse_bool, parse_number, split_date_time, yyyymmdd_to_iso)

CHUNK_ROWS = 50_000          # rows cleaned at a time
HEAD_ROWS = 200              # non-blank rows read to find the header
HEADER_SEARCH_ROWS = 50      # the header must be among the first 50 non-blank rows
MAX_LABEL_LENGTH = 120       # a header label is short; longer text is a note or a title
HEADER_FULLNESS = 0.7        # a header row has at least this share of a typical row's cells

# A column is a number / date / yes-no column when at least this share of its non-empty
# values can be read as one. The others become empty and are counted in the report.
TYPE_SHARE = 0.90
# ... unless its header says it holds identifiers or names. Then EVERY value must fit,
# because turning 2% of the product codes into empty cells would lose real products.
IDENTIFIER_ENDINGS = frozenset({
    "id", "code", "sku", "no", "num", "number", "nr", "ref", "key", "barcode", "upc", "ean",
    "zip", "postcode", "phone", "name", "description", "desc", "title",
})
# A number column stays text when this share of its values look like codes ("00123").
ID_LIKE_SHARE = 0.01
# Header words that allow a column of plain numbers to be read as dates (Excel day numbers
# such as 45000, or dates written as 20240105).
DATE_HEADER_WORDS = frozenset({"date", "day", "dt", "datetime", "timestamp", "counted", "created", "updated"})

# Total rows. EXACT labels always mark a total row. A label that only STARTS like one
# ("Total for March", but also the product "Total Care Shampoo") marks a total row only
# when the row has fewer filled cells than a normal row, which is how totals look.
_TOTAL_EXACT = frozenset({
    "total", "totals", "grand total", "grand totals", "subtotal", "sub total", "sub-total",
    "subtotals", "overall total", "sum", "report total", "page total",
})
_TOTAL_PREFIXES = ("total", "grand total", "subtotal", "sub total", "sub-total", "overall total")
NOTE_ROW_TABLE_WIDTH = 3     # single-cell rows are dropped only in tables at least this wide


class CleanTable:
    """One cleaned sheet: the data plus what is known about each column."""

    def __init__(self, name: str, source_file: str, source_sheet: str, frame: pd.DataFrame, columns: list):
        self.name = name                    # "t_sales"
        self.source_file = source_file      # cleaned file name, never a path
        self.source_sheet = source_sheet    # cleaned sheet name
        self.frame = frame                  # the cleaned rows
        self.columns = columns              # [{"name", "original_header", "dtype"}, ...]
        self.empty_headers = []             # headers of columns dropped because they had no values


class SheetResult:
    """What clean_sheet() hands back: a table, or the reason there is none."""

    def __init__(self, sheet_name: str):
        self.sheet_name = sheet_name    # cleaned sheet name, for messages
        self.table = None           # CleanTable when the sheet is usable
        self.problem = None         # "blank" | "header_only" | "one_column" when it is not
        self.steps = []             # (step, count, detail) for the cleaning report
        self.warnings = []          # sentences for the owner
        self.rows_read = 0          # data rows read, counted against the row limit


# ======================================================================================
# Step 1: the header
# ======================================================================================
def read_head(sheet: RawSheet) -> list:
    """The first HEAD_ROWS non-blank rows as (position, cells). `position` is the row's
    place in the sheet counted from 0; cells are cleaned text or None."""
    head = []
    rows = sheet.rows()
    try:
        for position, row in enumerate(rows):
            cells = [clean_cell(cell)[0] for cell in row]
            if any(cell is not None for cell in cells):
                head.append((position, cells))
                if len(head) >= HEAD_ROWS:
                    break
    finally:
        rows.close()
    return head


def _is_label(text: str) -> bool:
    """Could this cell be a column header? Headers are short text, not numbers or dates."""
    return (len(text) <= MAX_LABEL_LENGTH
            and number_shape(text) is None
            and not looks_like_date(text))


class Header:
    """Where the header is and what it says."""

    def __init__(self):
        self.found = False
        self.labels = []            # one label (or None) per column position
        self.label_rows = []        # the header row(s) as read, to spot repeats further down
        self.first_row = 0          # position of the first header row (for the report)
        self.row_count = 0          # 0, 1 or 2 header rows
        self.data_start = 0         # position of the first data row
        self.typical_filled = 1     # filled cells in a normal data row


def find_header(head: list) -> Header:
    """Find the header row among the first rows of a sheet.

    The rule, in words:
      * A WIDE row has at least half as many filled cells as a typical row, and at least 2.
        Narrow rows (a title, a date line, a note) are ignored.
      * A LABEL row is a wide row in which every filled cell is text: no numbers, no dates.
      * The data begins at the first wide row that is NOT a label row and is directly
        followed by another such row (or is the last wide row). One odd row with a number in
        it above the header, for example "Report for 2024", does not count as data.
      * The header is the first label row above that point that is nearly as full as a
        typical row (70% of its filled cells). A title such as "Acme Ltd | Sales report" is
        too short to qualify. If no label row is that full, the fullest one is taken.
      * A sheet with no numbers and no dates at all gives nothing to tell a title from a
        header except position, so there the first wide row is the header.
      * When there is no label row above the data, the sheet has no header and its columns
        are named column_1, column_2, ...
    A header that spans two rows (merged cells) is handled by _join_second_header_row.

    Known limit: a header whose labels are numbers or dates (years as column headers) is not
    recognised as a header; the sheet is then treated as having none.
    """
    header = Header()
    filled = [sum(cell is not None for cell in cells) for _position, cells in head]
    typical = int(statistics.median(filled))
    needed = max(2, math.ceil(typical / 2))
    header.typical_filled = typical

    wide = [index for index in range(len(head)) if filled[index] >= needed]
    is_label_row = {index: all(_is_label(cell) for cell in head[index][1] if cell is not None)
                    for index in wide}

    # Where does the data begin?
    data_index = None
    for place, index in enumerate(wide):
        if is_label_row[index]:
            continue
        is_last = place == len(wide) - 1
        if is_last or not is_label_row[wide[place + 1]]:
            data_index = index
            break

    candidates = [index for index in wide
                  if is_label_row[index] and index < HEADER_SEARCH_ROWS
                  and (data_index is None or index < data_index)]
    if not candidates:
        # No header. The data starts at the first wide row (or the first row of all).
        first = wide[0] if wide else 0
        header.data_start = head[first][0]
        header.first_row = header.data_start
        return header

    nearly_full = max(2, math.ceil(typical * HEADER_FULLNESS))
    full_enough = [index for index in candidates if filled[index] >= nearly_full]
    if data_index is None:
        best = candidates[0]                              # text-only sheet: position decides
    elif full_enough:
        best = full_enough[0]
    else:
        best = max(candidates, key=lambda index: (filled[index], -index))    # fullest; earliest on ties
    header.found = True
    _join_second_header_row(header, head, best, filled)

    after = [filled[index] for index in range(len(head)) if head[index][0] >= header.data_start]
    if after:
        header.typical_filled = int(statistics.median(after))
    return header


def _join_second_header_row(header: Header, head: list, best: int, filled: list) -> None:
    """Decide whether the header has one row or two, and build the labels.

    Merged header cells look like this once the merge is gone (the text sits in the first
    cell, the others are empty):

        | Product | Sales |         | Stock   |         |      <- upper row
        |         | Units | Revenue | On hand | Reorder |      <- lower row

    "Product" is merged downwards, so the lower row is empty under it. "Sales" is merged
    to the right, so the upper row is empty next to it.

    The chosen row and a row directly above or below it are joined when:
      * ABOVE: that row has two or more cells, and the chosen row either repeats a label
        (Units, Revenue, Units, Revenue) or has an empty cell that the row above fills.
        An empty cell counts only inside the chosen row or over a column that has data;
        a stray title cell far to the right does not.
      * BELOW: that row is all text, fills an empty cell of the chosen row, and is itself
        empty under one of the chosen row's labels (the sign of a cell merged downwards).
        Without that last condition a first data row made of text would be taken for a
        second header row.
    An empty upper cell takes the label to its left. Joined names read "sales_units".
    """
    position, cells = head[best]
    upper, lower = None, None

    if best > 0 and head[best - 1][0] == position - 1:
        above = head[best - 1][1]
        labels = [cell.lower() for cell in cells if cell is not None]
        repeats = len(labels) != len(set(labels))
        span = max(index for index, cell in enumerate(cells) if cell is not None) + 1
        columns_with_data = {index for _position, row in head[best + 1:]
                             for index, cell in enumerate(row) if cell is not None}
        fills_gap = any(_cell(cells, index) is None and above[index] is not None
                        and (index < span or index in columns_with_data)
                        for index in range(len(above)))
        if filled[best - 1] >= 2 and (repeats or fills_gap):
            upper, lower = best - 1, best

    if upper is None and best + 1 < len(head) and head[best + 1][0] == position + 1:
        below = head[best + 1][1]
        below_is_labels = filled[best + 1] >= 2 and all(_is_label(cell) for cell in below if cell is not None)
        fills_gap = any(_cell(cells, index) is None and below[index] is not None for index in range(len(below)))
        merged_down = any(cells[index] is not None and _cell(below, index) is None for index in range(len(cells)))
        if below_is_labels and fills_gap and merged_down:
            upper, lower = best, best + 1

    if upper is None:
        header.labels = list(cells)
        header.label_rows = [list(cells)]
        header.first_row = position
        header.row_count = 1
        header.data_start = position + 1
        return

    top, bottom = head[upper][1], head[lower][1]
    labels = []
    carried = None                                    # the group label being carried right
    for index in range(max(len(top), len(bottom))):
        group, own = _cell(top, index), _cell(bottom, index)
        if group is not None:
            carried = group
        elif own is not None:
            group = carried                           # empty upper cell: part of a merged cell
        if group is not None and own is not None and group.lower() != own.lower():
            labels.append(f"{group} {own}")
        else:
            labels.append(own if own is not None else group)
    header.labels = labels
    header.label_rows = [list(top), list(bottom)]
    header.first_row = head[upper][0]
    header.row_count = 2
    header.data_start = head[lower][0] + 1


def _cell(cells: list, index: int):
    return cells[index] if index < len(cells) else None


# ======================================================================================
# Reading and cleaning rows, one chunk at a time (used by both passes)
# ======================================================================================
def _raw_chunks(sheet: RawSheet, data_start: int):
    """Yield lists of at most CHUNK_ROWS raw rows, starting at the first data row."""
    batch = []
    rows = sheet.rows()
    try:
        for position, row in enumerate(rows):
            if position < data_start:
                continue
            batch.append(row)
            if len(batch) >= CHUNK_ROWS:
                yield batch
                batch = []
        if batch:
            yield batch
    finally:
        rows.close()


def _total_label_kind(text: str) -> int:
    """0: not a total label, 1: starts like one ("Total for March"), 2: exactly one ("Total")."""
    label = text.lower().rstrip(" :")
    if label in _TOTAL_EXACT:
        return 2
    if label.startswith(_TOTAL_PREFIXES):
        return 1
    return 0


def _clean_column(values: np.ndarray, counts: Counter) -> tuple:
    """Clean every cell of one column of a chunk.

    Returns (cleaned, total_kinds): the cleaned cells, and for each cell 0 / 1 / 2 from
    _total_label_kind. clean_cell runs once per distinct value; `counts` receives how many
    cells each cleaning action touched.
    """
    codes, uniques, weights = distinct_values(values)
    # One extra slot at the end stays None / 0. Empty cells have code -1, and index -1 picks
    # that last slot, so empty cells come out as None without a special case.
    cleaned = np.empty(len(uniques) + 1, dtype=object)
    kinds = np.zeros(len(uniques) + 1, dtype=np.int8)
    for index, raw in enumerate(uniques):
        text, notes = clean_cell(raw)
        cleaned[index] = text
        for note in notes:
            counts[note] += int(weights[index])
        if text is not None and text[0] in "TtGgSsOoRrPp":       # cheap first-letter filter
            kinds[index] = _total_label_kind(text)
    return cleaned[codes], kinds[codes]


def _clean_chunk(batch: list, width, header: Header, counts: Counter) -> list:
    """Clean one chunk of raw rows and drop the rows that are not data.

    Returns the columns of the chunk as a list of numpy arrays (cleaned text or None).
    Dropped rows are counted in `counts`: blank rows, repeats of the header, total rows,
    and single-cell note rows.
    """
    # Turn rows into columns. zip_longest pads short rows with None.
    raw_columns = list(zip_longest(*batch, fillvalue=None))
    if width is not None:
        raw_columns = raw_columns[:width]
    columns, kinds = [], []
    for raw in raw_columns:
        values = np.empty(len(batch), dtype=object)
        values[:] = raw
        cleaned, kind = _clean_column(values, counts)
        columns.append(cleaned)
        kinds.append(kind)
    row_count = len(batch)
    if not columns:
        counts["blank_rows"] += row_count
        return []

    filled = np.zeros(row_count, dtype=np.int64)
    for column in columns:
        filled += pd.notna(column)
    blank = filled == 0
    repeated = _repeated_header_rows(columns, header) & ~blank

    exact_total = np.zeros(row_count, dtype=bool)
    prefix_total = np.zeros(row_count, dtype=bool)
    for kind in kinds:
        exact_total |= kind == 2
        prefix_total |= kind == 1
    total = (exact_total | (prefix_total & (filled < header.typical_filled))) & ~blank & ~repeated

    # A row with a single filled cell, in a table whose rows normally have three or more,
    # is a note or a section label ("Region: East", "Prices exclude tax"), not a record.
    note = (filled == 1) & (header.typical_filled >= NOTE_ROW_TABLE_WIDTH) & ~repeated & ~total

    counts["blank_rows"] += int(blank.sum())
    counts["repeated_header_rows"] += int(repeated.sum())
    counts["total_rows"] += int(total.sum())
    counts["note_rows"] += int(note.sum())
    keep = ~(blank | repeated | total | note)
    if keep.all():
        return columns
    return [column[keep] for column in columns]


def _repeated_header_rows(columns: list, header: Header) -> np.ndarray:
    """Mark rows that repeat the header (exports often print it again on every page).
    A row is a repeat when each of its filled cells equals the header label above it."""
    row_count = len(columns[0])
    marked = np.zeros(row_count, dtype=bool)
    for label_row in header.label_rows:
        labelled = [(index, label.lower()) for index, label in enumerate(label_row)
                    if label is not None and index < len(columns)]
        if len(labelled) < 2:
            continue
        # Quick filter on the first labelled column, then check the few rows that pass.
        first_index, first_label = labelled[0]
        suspects = [row for row, cell in enumerate(columns[first_index].tolist())
                    if cell is not None and cell.lower() == first_label]
        for row in suspects:
            cells = [(columns[index][row], label) for index, label in labelled]
            present = [(cell, label) for cell, label in cells if cell is not None]
            if len(present) >= 2 and all(cell.lower() == label for cell, label in present):
                marked[row] = True
    return marked


# ======================================================================================
# Pass 1: look at every value and decide each column's type
# ======================================================================================
class ColumnProfile:
    """Counts collected for one column while reading the whole sheet once."""

    def __init__(self):
        self.non_null = 0            # cells with a value
        self.bool_ok = 0             # ... that are yes / no
        self.us_ok = 0               # ... that are numbers read the US way (1,234.50)
        self.eu_ok = 0               # ... that are numbers read the European way (1.234,50)
        self.us_votes = 0            # values that can ONLY be read the US way
        self.eu_votes = 0            # values that can ONLY be read the European way
        self.id_like = 0             # numbers that look like codes (00123)
        self.plain_count = 0         # numbers written as plain digits
        self.plain_min = math.inf
        self.plain_max = -math.inf
        self.plain_fraction = False  # some plain number has a fractional part
        self.date_ok = {}            # date layout -> how many values fit it
        self.date_any = 0            # values that fit at least one date layout
        self.month_first_only = 0    # values that can only be month/day/year (12/25/2024)
        self.day_first_only = 0      # values that can only be day/month/year (25/12/2024)
        self.either_order = 0        # values that can be read both ways (03/04/2024)
        self.has_time = False        # some date carries a time of day

    def add(self, values: np.ndarray) -> None:
        """Count one chunk of cleaned values."""
        _codes, uniques, weights = distinct_values(values)
        self.non_null += int(weights.sum())
        date_texts, date_weights = [], []
        for text, weight in zip(uniques, weights.tolist()):
            shape = number_shape(text)
            if shape is not None:
                us_ok, eu_ok, vote, id_like, plain = shape
                self.us_ok += weight if us_ok else 0
                self.eu_ok += weight if eu_ok else 0
                self.us_votes += weight if vote == "us" else 0
                self.eu_votes += weight if vote == "eu" else 0
                self.id_like += weight if id_like else 0
                if plain is not None:
                    self.plain_count += weight
                    self.plain_min = min(self.plain_min, plain)
                    self.plain_max = max(self.plain_max, plain)
                    self.plain_fraction = self.plain_fraction or plain != int(plain)
            elif parse_bool(text) is not None:
                self.bool_ok += weight
            elif looks_like_date(text):
                date_texts.append(text)
                date_weights.append(weight)
        self._add_dates(date_texts, date_weights)

    def _add_dates(self, texts: list, weights: list) -> None:
        """Count, for every supported date layout, how many values fit it.

        EVERY date-shaped value is tried against EVERY layout in DATE_FORMATS, so the
        counts do not depend on how the rows happen to be split into chunks, and a layout
        that only a few values use is found as surely as the main one. This stays cheap
        because the time of day is split off first: a column of a million date-and-time
        values holds only a few hundred distinct dates.
        """
        if not texts:
            return
        weight_of = {}                                   # distinct date text -> rows that hold it
        for text, weight in zip(texts, weights):
            date_text, time_text = split_date_time(text)
            if time_text not in (None, "00:00:00"):
                self.has_time = True
            weight_of[date_text] = weight_of.get(date_text, 0) + weight
        date_texts = list(weight_of)
        weight_array = np.array([weight_of[date_text] for date_text in date_texts])

        fits = {}                                        # layout -> which of the dates fit it
        fits_any = np.zeros(len(date_texts), dtype=bool)
        for fmt in DATE_FORMATS:
            fits[fmt] = np.array([day is not None for day in dates_to_iso(date_texts, fmt)], dtype=bool)
            self.date_ok[fmt] = self.date_ok.get(fmt, 0) + int(weight_array[fits[fmt]].sum())
            fits_any |= fits[fmt]
        self.date_any += int(weight_array[fits_any].sum())

        # Month first or day first? Count the evidence: 12/25/2024 can only be month first,
        # 25/12/2024 only day first, 03/04/2024 says nothing.
        for month_first, day_first in AMBIGUOUS_PAIRS.items():
            self.month_first_only += int(weight_array[fits[month_first] & ~fits[day_first]].sum())
            self.day_first_only += int(weight_array[fits[day_first] & ~fits[month_first]].sum())
            self.either_order += int(weight_array[fits[month_first] & fits[day_first]].sum())

    def _date_layouts(self) -> list:
        """The layouts found in this column, in the order in which each value is tried.

        Only the month-first / day-first pairs can both fit the same text, so only their
        order matters. It is decided ONCE for the whole column from the values that can be
        read one way only: day first when more values demand it than demand month first,
        otherwise month first (also when there is no evidence at all).
        """
        day_first = self.day_first_only > self.month_first_only
        swap = dict(AMBIGUOUS_PAIRS)
        swap.update({day: month for month, day in AMBIGUOUS_PAIRS.items()})
        layouts = []
        for fmt in DATE_FORMATS:
            if day_first and fmt in swap:
                fmt = swap[fmt]                          # take the pair in the opposite order
            if self.date_ok.get(fmt, 0) > 0:
                layouts.append(fmt)
        return layouts

    def decide(self, header_key: str) -> dict:
        """Choose the column's type from the counts. Returns the conversion plan used by
        pass 2: {"dtype", "decimal_comma", "date_formats", "has_time", "special", "ambiguous",
        "mixed_orders", "day_first"}."""
        plan = {"dtype": "text", "decimal_comma": False, "date_formats": [], "has_time": self.has_time,
                "special": None, "ambiguous": False, "mixed_orders": False, "day_first": False}
        if self.non_null == 0:
            return plan
        tokens = header_key.split("_")
        strict = tokens[-1] in IDENTIFIER_ENDINGS
        needed = self.non_null if strict else math.ceil(self.non_null * TYPE_SHARE)

        if self.bool_ok >= needed:
            plan["dtype"] = "bool"
            return plan

        # Numbers. The column is read the European way only when more values demand it
        # than demand the US way.
        decimal_comma = self.eu_votes > self.us_votes
        number_ok = self.eu_ok if decimal_comma else self.us_ok
        looks_like_codes = self.id_like >= max(1, number_ok * ID_LIKE_SHARE)
        if number_ok >= needed and not looks_like_codes:
            plan["dtype"] = "number"
            plan["decimal_comma"] = decimal_comma
            if DATE_HEADER_WORDS.intersection(tokens) and self.plain_count == number_ok:
                if (not self.plain_fraction
                        and YYYYMMDD_RANGE[0] <= self.plain_min and self.plain_max <= YYYYMMDD_RANGE[1]):
                    plan.update(dtype="date", special="yyyymmdd", has_time=False)
                elif EXCEL_SERIAL_RANGE[0] <= self.plain_min and self.plain_max <= EXCEL_SERIAL_RANGE[1]:
                    plan.update(dtype="date", special="excel_serial", has_time=self.plain_fraction)
            return plan

        # Dates. A column may mix layouts ("5-Feb-2024" next to "2024-02-06"), so what counts
        # is how many values fit ANY supported layout. Each value is later read with the
        # first layout of _date_layouts() that fits it.
        date_needed = math.ceil(self.non_null * TYPE_SHARE)           # a date header is never "strict"
        if self.date_any > 0 and self.date_any >= date_needed:
            undecided = self.month_first_only == self.day_first_only
            plan.update(
                dtype="date", date_formats=self._date_layouts(),
                day_first=self.day_first_only > self.month_first_only,
                # No value says which order is meant (or the evidence is split evenly), and
                # some values can be read both ways: they are read month first, with a warning.
                ambiguous=undecided and self.either_order > 0,
                # Some values are clearly month first and others clearly day first.
                mixed_orders=not undecided and min(self.month_first_only, self.day_first_only) > 0)
        return plan


# ======================================================================================
# Pass 2: convert each column to its type
# ======================================================================================
def _convert_column(values: np.ndarray, plan: dict, failures: dict) -> np.ndarray:
    """Convert one chunk of one column. Text columns come back unchanged; other columns come
    back as numbers (float with NaN for empty) or as ISO date text. Values that do not fit
    become empty; `failures` collects how many, with a few examples for the report."""
    dtype = plan["dtype"]
    if dtype == "text":
        return values
    codes, uniques, weights = distinct_values(values)
    if dtype == "date":
        converted = _convert_dates(uniques, plan)
        result = np.empty(len(uniques) + 1, dtype=object)       # last slot: empty cells
        result[:len(uniques)] = converted
    else:
        result = np.full(len(uniques) + 1, np.nan)               # last slot: empty cells
        for index, text in enumerate(uniques):
            if dtype == "bool":
                value = parse_bool(text)
            else:
                value = parse_number(text, plan["decimal_comma"])
            if value is not None:
                result[index] = value
        converted = [None if value != value else value for value in result[:len(uniques)].tolist()]
    for index, value in enumerate(converted):
        if value is None:
            failures["count"] += int(weights[index])
            if len(failures["examples"]) < 3:
                failures["examples"].append(str(uniques[index])[:30])
    return result[codes]


def _convert_dates(uniques, plan: dict) -> list:
    """ISO text for each distinct value of a date column (None where it is not a date)."""
    if plan["special"] is not None:
        result = []
        for text in uniques:
            number = parse_number(text)
            if number is None:
                result.append(None)
            elif plan["special"] == "yyyymmdd":
                result.append(yyyymmdd_to_iso(number))
            else:
                result.append(excel_serial_to_iso(number, plan["has_time"]))
        return result
    parts = [split_date_time(text) for text in uniques]
    date_texts = [date for date, _time in parts]
    days = [None] * len(date_texts)
    for fmt in plan["date_formats"]:
        unread = [index for index, day in enumerate(days) if day is None]
        if not unread:
            break
        for index, day in zip(unread, dates_to_iso([date_texts[index] for index in unread], fmt)):
            days[index] = day
    if not plan["has_time"]:
        return days
    return [None if day is None else f"{day} {time or '00:00:00'}"
            for day, (_date, time) in zip(days, parts)]


# ======================================================================================
# The whole job for one sheet
# ======================================================================================
def clean_sheet(sheet: RawSheet, table_name: str, source_file: str, rows_allowed: int) -> SheetResult:
    """Clean one sheet. `rows_allowed` is what is left of the upload's row limit; reading
    more rows than that raises InvalidFile("too_many_rows")."""
    result = SheetResult(clean_label(sheet.name))
    head = read_head(sheet)
    if not head:
        result.problem = "blank"
        return result
    header = find_header(head)

    # ---- pass 1: count -------------------------------------------------------------
    profiles = []
    kept_rows = 0
    for batch in _raw_chunks(sheet, header.data_start):
        dropped = Counter()
        columns = _clean_chunk(batch, None, header, dropped)
        result.rows_read += len(batch) - dropped["blank_rows"]       # blank rows are free
        if result.rows_read > rows_allowed:
            raise InvalidFile("too_many_rows", "")          # prepare() writes the sentence
        while len(profiles) < len(columns):
            profiles.append(ColumnProfile())
        for profile, column in zip(profiles, columns):
            profile.add(column)
        kept_rows += len(columns[0]) if columns else 0

    width = max(len(profiles), len(header.labels))
    while len(profiles) < width:
        profiles.append(ColumnProfile())
    labels = [_cell(header.labels, index) for index in range(width)]
    names = unique_names([normalise_header(label) for label in labels])
    plans = [profile.decide(normalise_header(label)) for profile, label in zip(profiles, labels)]
    used = [index for index in range(width) if profiles[index].non_null > 0]

    _report_header(result, header, labels, names, used, width)
    if sheet.columns_cut:
        result.steps.append(("columns_ignored", 1, f"The sheet has more than {MAX_COLUMNS} columns; "
                                                   f"only the first {MAX_COLUMNS} were read."))
        result.warnings.append(f"Sheet '{result.sheet_name}' has more than {MAX_COLUMNS} columns; the rest were ignored.")
    if kept_rows == 0:
        result.problem = "header_only"
        return result
    if len(used) < 2:
        result.problem = "one_column"
        return result

    # ---- pass 2: convert -----------------------------------------------------------
    counts = Counter()
    failures = {index: {"count": 0, "examples": []} for index in used}
    pieces = {index: [] for index in used}
    for batch in _raw_chunks(sheet, header.data_start):
        columns = _clean_chunk(batch, width, header, counts)
        if not columns:
            continue
        rows_in_chunk = len(columns[0])
        for index in used:
            if index < len(columns):
                values = columns[index]
            else:                                            # a chunk with fewer columns
                values = np.full(rows_in_chunk, None, dtype=object)
            pieces[index].append(_convert_column(values, plans[index], failures[index]))

    frame = _build_frame(pieces, plans, names, used)
    duplicates = frame.duplicated().to_numpy()
    if duplicates.any():
        frame = frame[~duplicates].reset_index(drop=True)
    columns_info = _finish_types(frame, plans, names, labels, used)

    _report_cleaning(result, counts, failures, plans, names, columns_info, int(duplicates.sum()), len(frame))
    result.table = CleanTable(table_name, source_file, result.sheet_name, frame, columns_info)
    result.table.empty_headers = [labels[index] for index in range(width)
                                  if index not in used and labels[index] is not None]
    return result


def _build_frame(pieces: dict, plans: list, names: list, used: list) -> pd.DataFrame:
    """Join the converted chunks of every column into one DataFrame."""
    data = {}
    for index in used:
        joined = np.concatenate(pieces[index]) if pieces[index] else np.array([], dtype=object)
        if plans[index]["dtype"] in ("number", "bool"):
            data[names[index]] = pd.Series(joined, dtype="float64")
        else:
            # dtype=object is given on purpose: text columns hold str and None in every
            # pandas version, whatever its default string type is.
            data[names[index]] = pd.Series(joined, dtype=object)
    return pd.DataFrame(data)


def _finish_types(frame: pd.DataFrame, plans: list, names: list, labels: list, used: list) -> list:
    """Give whole-number columns an integer type and describe every column."""
    columns_info = []
    for index in used:
        name, dtype = names[index], plans[index]["dtype"]
        if dtype in ("number", "bool"):
            numbers = frame[name].to_numpy(dtype="float64")
            present = numbers[~np.isnan(numbers)]
            whole = present.size > 0 and bool(np.all(present == np.floor(present))) \
                and float(np.abs(present).max()) < 2 ** 53
            if dtype == "bool" or whole:
                frame[name] = frame[name].astype("Int64")        # integers that allow empty cells
                dtype = "bool" if dtype == "bool" else "integer"
        columns_info.append({"name": name,
                             "original_header": clean_label(labels[index]) or "",
                             "dtype": dtype})
    return columns_info


# ======================================================================================
# The cleaning report for one sheet
# ======================================================================================
def _report_header(result: SheetResult, header: Header, labels: list, names: list, used: list, width: int) -> None:
    steps = result.steps
    if header.found:
        row_text = f"row {header.first_row + 1}" if header.row_count == 1 \
            else f"rows {header.first_row + 1} and {header.first_row + 2} (joined)"
        steps.append(("header_row", header.first_row + 1, f"The column headers were found on {row_text}."))
        if header.row_count == 2:
            steps.append(("merged_header", 1, "The header spans two rows; the two labels of each column were joined."))
        if header.first_row > 0:
            steps.append(("rows_above_header", header.first_row,
                          "Rows above the header (titles, notes, blank lines) were skipped."))
    else:
        steps.append(("no_header", 1, "No header row was found; the columns were named column_1, column_2, ..."))
        result.warnings.append(f"Sheet '{result.sheet_name}' has no header row, "
                               "so its columns could not be recognised by name.")
        if header.data_start > 0:
            steps.append(("rows_above_header", header.data_start, "Rows above the first data row were skipped."))

    redacted = sum(1 for label in labels if label in (REMOVED_INSTRUCTION, REMOVED_LINK))
    if redacted:
        steps.append(("redacted_headers", redacted,
                      "Headers that looked like instructions or links were replaced before naming the columns."))
    renamed = [(labels[index], names[index]) for index in used
               if labels[index] is not None and labels[index] != names[index]]
    if renamed:
        examples = ", ".join(f"'{clean_label(old, 30)}' -> {new}" for old, new in renamed[:4])
        steps.append(("headers_normalised", len(renamed), f"Header names were tidied, for example {examples}."))
    empty = width - len(used)
    if empty:
        steps.append(("empty_columns", empty, "Columns with no values were dropped."))


_CELL_STEPS = [
    ("blank_rows", "blank_rows", "Fully blank rows were dropped."),
    ("repeated_header_rows", "repeated_header_rows", "Rows that repeat the header were dropped."),
    ("total_rows", "total_rows", "Total and subtotal rows were dropped."),
    ("note_rows", "note_rows", "Rows with a single filled cell (notes or section labels) were dropped."),
    ("whitespace", "whitespace_trimmed", "Cells had extra spaces or line breaks removed."),
    ("control_characters", "control_characters_removed", "Cells had invisible or control characters removed."),
    ("null_marker", "null_markers", "Cells such as 'N/A' or '#DIV/0!' were treated as empty."),
    ("instruction", "redacted_instruction_text", f"Cells that looked like instructions to an AI model were replaced with '{REMOVED_INSTRUCTION}'."),
    ("link", "redacted_links", f"Cells holding links or HTML were replaced with '{REMOVED_LINK}'."),
    ("cut", "text_cut", "Long text cells were cut to 200 characters."),
]


def _report_cleaning(result: SheetResult, counts: Counter, failures: dict, plans: list, names: list,
                     columns_info: list, duplicates: int, rows_kept: int) -> None:
    steps = result.steps
    for key, step, detail in _CELL_STEPS:
        if counts[key]:
            steps.append((step, counts[key], detail))
    typed = [f"{info['name']}: {info['dtype']}" for info in columns_info if info["dtype"] != "text"]
    if typed:
        shown = ", ".join(typed[:12]) + (" ..." if len(typed) > 12 else "")
        steps.append(("column_types", len(typed), f"Columns converted from text: {shown}."))
    for index, failure in failures.items():
        if failure["count"]:
            examples = ", ".join(f"'{example}'" for example in failure["examples"])
            steps.append(("unconvertible_values", failure["count"],
                          f"Column {names[index]}: values that are not a {plans[index]['dtype']} "
                          f"were left empty, for example {examples}."))
        if plans[index]["ambiguous"]:
            steps.append(("ambiguous_dates", 1,
                          f"Column {names[index]}: dates such as 03/04/2024 can be read two ways; "
                          "they were read as month/day/year."))
            result.warnings.append(f"Dates in column {names[index]} were read as month/day/year. "
                                   "If they are day/month/year, change the date format in the file.")
        if plans[index]["mixed_orders"]:
            order = "day/month/year" if plans[index]["day_first"] else "month/day/year"
            steps.append(("mixed_date_orders", 1,
                          f"Column {names[index]} holds both month-first and day-first dates; dates that "
                          f"can be read either way were read as {order}, the more common order."))
            result.warnings.append(f"Column {names[index]} mixes month-first and day-first dates. Dates such as "
                                   f"03/04/2024 were read as {order}; check them.")
        if plans[index]["special"] == "excel_serial":
            steps.append(("excel_serial_dates", 1, f"Column {names[index]}: Excel day numbers were converted to dates."))
        if plans[index]["decimal_comma"]:
            steps.append(("decimal_comma", 1, f"Column {names[index]}: numbers were read with a decimal comma (1.234,50)."))
    if duplicates:
        steps.append(("duplicate_rows", duplicates, "Rows that were exact copies of an earlier row were dropped."))
    steps.append(("rows_kept", rows_kept, "Data rows kept after cleaning."))
