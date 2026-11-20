"""
Reading single values: is this text a number, a yes/no answer or a date, and what is it worth?

Every function here takes ONE cleaned text value (or, for dates, a list of distinct values)
and has no knowledge of files or tables, so each rule can be tested on its own:

    parse_number("$1,234.50")                      -> 1234.5
    parse_number("(45.00)")                        -> -45.0
    parse_number("1.234,50", decimal_comma=True)   -> 1234.5
    parse_bool("Yes")                              -> 1
    split_date_time("12/1/2010 8:26")              -> ("12/1/2010", "08:26:00")

Nothing in this module ever evaluates text as code. A cell such as "=1+1" or "@name" is
simply not a number and stays text.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------------
# Doing work once per distinct value
# --------------------------------------------------------------------------------------

def distinct_values(values: np.ndarray) -> tuple:
    """Split a column into (codes, uniques, weights).

    `uniques` are the distinct non-empty values, `codes` says which unique each row holds
    (-1 for an empty cell) and `weights` says how many rows hold each unique. A column of a
    million rows often has only a few thousand distinct values, so the slow per-value rules
    in this package run on `uniques` and the answers are put back with `codes`.
    """
    codes, uniques = pd.factorize(values)
    weights = np.bincount(codes[codes >= 0], minlength=len(uniques))
    return codes, uniques, weights


# --------------------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------------------
_CURRENCY_SYMBOLS = str.maketrans("", "", "$€£¥₹₩₽¢")
_CURRENCY_CODE = re.compile(
    r"^(?:USD|EUR|GBP|CAD|AUD|NZD|JPY|INR|CHF|CNY|MXN|PHP|SGD|HKD|ZAR|BRL|US|CA|AU)\s*(?=[\d.,(\-+])"
    r"|(?<=[\d.,)])\s*(?:USD|EUR|GBP|CAD|AUD|NZD|JPY|INR|CHF|CNY|MXN|PHP|SGD|HKD|ZAR|BRL)$",
    re.IGNORECASE,
)
# A space or apostrophe used as a thousands separator: "1 234 567" or "1'234".
_GROUP_SPACE = re.compile(r"(?<=\d)[ '](?=\d{3}(?!\d))")

# Shapes of a number once the decorations are gone.
#   US / UK style:   1,234.50   1234.5   .5      (comma groups thousands, dot is the decimal)
#   European style:  1.234,50   1234,5   ,5      (dot groups thousands, comma is the decimal)
#   Scientific:      1.23E+05   (only with an explicit sign, as Excel writes it, so that a
#                    product code such as "12E45" is not mistaken for a number)
_US_NUMBER = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?\Z|\.\d+\Z")
_EU_NUMBER = re.compile(r"(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d*)?\Z|,\d+\Z")
_SCIENTIFIC = re.compile(r"\d+(?:\.\d+)?[eE][+-]\d{1,3}\Z")

_ONE_COMMA_GROUP = re.compile(r"\d{1,3},\d{3}\Z")      # 1,234   (could be either style)
_ONE_DOT_GROUP = re.compile(r"\d{1,3}\.\d{3}\Z")       # 1.234   (could be either style)

MAX_SAFE_DIGITS = 15     # longer digit strings (barcodes, card numbers) lose digits as numbers


def _number_core(text: str):
    """Remove what people put around a number: brackets for negatives, currency signs and
    codes, a percent sign, a leading or trailing minus. Returns (is_negative, digits) or None."""
    core = text.replace("−", "-")                   # the typographic minus sign
    core = core.translate(_CURRENCY_SYMBOLS).strip()     # $  €  £ ... anywhere in the text
    negative = False
    if core.startswith("(") and core.endswith(")"):      # accountants write (45.00) for -45.00
        negative = True
        core = core[1:-1].strip()
    core = _CURRENCY_CODE.sub("", core).strip()          # USD 45.00   45.00 EUR
    if core.endswith("%"):
        core = core[:-1].strip()
    if core.endswith("-"):                               # some systems write 45.00-
        negative = not negative
        core = core[:-1].strip()
    elif core.startswith("-"):
        negative = not negative
        core = core[1:].strip()
    elif core.startswith("+"):
        core = core[1:].strip()
    core = _GROUP_SPACE.sub("", core)
    if not core or core[0] not in "0123456789.,":
        return None
    return negative, core


def parse_number(text: str, decimal_comma: bool = False):
    """The number written in `text`, or None when it is not a number.

    `decimal_comma` chooses how "1.234" and "1,234" are read. It is decided once per column
    (see number_shape) because a single value can be ambiguous.
    A percent sign is dropped and the figure kept: "12%" is 12.0, not 0.12.
    """
    if text.isascii() and text.isdigit():                # the common case, kept fast
        return float(text) if len(text) <= MAX_SAFE_DIGITS else None
    parts = _number_core(text)
    if parts is None:
        return None
    negative, core = parts
    if _SCIENTIFIC.match(core):
        digits = core
    elif decimal_comma:
        if not _EU_NUMBER.match(core):
            return None
        digits = core.replace(".", "").replace(",", ".")
    else:
        if not _US_NUMBER.match(core):
            return None
        digits = core.replace(",", "")
    if not any(ch.isdigit() for ch in digits):
        return None
    number = float(digits)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return -number if negative else number


def number_shape(text: str):
    """Describe a value for the column-level decision "is this column made of numbers?".

    Returns None when the value is not a number in either style, otherwise a tuple
    (us_ok, eu_ok, vote, id_like, plain):
      us_ok / eu_ok  the value is a number when read in that style,
      vote           "us" or "eu" when the value can only be read one way, else None,
      id_like        True for values that look like codes rather than amounts: a leading
                     zero ("00123") or more than 15 digits. Turning those into numbers
                     would destroy them,
      plain          the value as a float when it is written as plain digits (used to
                     recognise Excel serial dates and yyyymmdd dates), else None.
    """
    if text.isascii() and text.isdigit():
        id_like = (len(text) > 1 and text[0] == "0") or len(text) > MAX_SAFE_DIGITS
        return True, True, None, id_like, float(text)
    parts = _number_core(text)
    if parts is None:
        return None
    _negative, core = parts
    if _SCIENTIFIC.match(core):
        return True, True, None, False, None
    us_ok = _US_NUMBER.match(core) is not None
    eu_ok = _EU_NUMBER.match(core) is not None
    if not (us_ok or eu_ok) or not any(ch.isdigit() for ch in core):
        return None
    vote = None
    if us_ok and not eu_ok:
        vote = "us"
    elif eu_ok and not us_ok:
        vote = "eu"
    # Both styles accept the value. "1,234,567" and "1.234.567" still say which style it is;
    # "1,234" and "1.234" do not, and "1234" has nothing to say.
    elif "," in core and not _ONE_COMMA_GROUP.match(core):
        vote = "us"
    elif "." in core and not _ONE_DOT_GROUP.match(core):
        vote = "eu"
    id_like = len(core) > 1 and core[0] == "0" and core.isdigit()
    plain = None
    if core == text and core.replace(".", "", 1).isdigit():
        plain = float(core)
    return us_ok, eu_ok, vote, id_like, plain


# --------------------------------------------------------------------------------------
# Yes / no
# --------------------------------------------------------------------------------------
_TRUE_WORDS = frozenset({"yes", "y", "true"})
_FALSE_WORDS = frozenset({"no", "n", "false"})


def parse_bool(text: str):
    """1 for yes / y / true, 0 for no / n / false (any letter case), None for anything else.
    The digits 1 and 0 are left alone: a column of 1s and 0s is a number column."""
    if len(text) > 5:
        return None
    word = text.lower()
    if word in _TRUE_WORDS:
        return 1
    if word in _FALSE_WORDS:
        return 0
    return None


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------
# The date layouts that are understood. A column may mix several of them; every value is
# read with whichever layout fits it. Only the pairs in AMBIGUOUS_PAIRS can fit the same
# text (03/04/2024 is March 4 or April 3): the month-first one is listed first and wins
# unless the column's other values show that it is written day first (see
# cleaning.ColumnProfile._date_layouts). The caller adds a warning when nothing decides it.
DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%m/%d/%y", "%d/%m/%y", "%d.%m.%y",
    "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %B %Y",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
]
# Pairs that read the same digits in a different order.
AMBIGUOUS_PAIRS = {"%m/%d/%Y": "%d/%m/%Y", "%m-%d-%Y": "%d-%m-%Y", "%m/%d/%y": "%d/%m/%y"}

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_DATE_SHAPE = re.compile(
    r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}(?!\d)"             # 2024-01-05   1/5/2024   05.01.24
    r"|\d{1,2}[- ]" + _MONTH + r"[- ,]+\d{2,4}(?!\d)"    # 5-Jan-2024   5 January 2024
    r"|" + _MONTH + r"\.? \d{1,2},? \d{2,4}(?!\d)",      # Jan 5, 2024
    re.IGNORECASE,
)
# A time of day at the end of the text, with an optional time-zone marker that is dropped
# (the clock time as written is kept; nothing is shifted between zones).
_TIME_TAIL = re.compile(
    r"^(?P<date>.*?\d)[ T]+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2})(?:\.\d+)?)?"
    r"\s*(?P<ampm>[AaPp][Mm])?\s*(?:Z|UTC|GMT|[+-]\d{2}:?\d{2})?$"
)

EXCEL_SERIAL_RANGE = (20000, 80000)          # 1954-10-03 .. 2119-01-12 as Excel day numbers
YYYYMMDD_RANGE = (19000101, 21001231)


def looks_like_date(text: str) -> bool:
    """A cheap test: does the text start the way a date does? Used to decide which values
    are worth trying against DATE_FORMATS, and to tell header labels from data."""
    return _DATE_SHAPE.match(text) is not None


def split_date_time(text: str) -> tuple:
    """Separate "12/1/2010 8:26 PM" into ("12/1/2010", "20:26:00").

    Returns (date_text, time_text). time_text is None when there is no time of day, and
    the whole text is returned as date_text when the ending is not a valid time.
    """
    found = _TIME_TAIL.match(text)
    if found is None:
        return text, None
    hour, minute = int(found["hour"]), int(found["minute"])
    second = int(found["second"] or 0)
    ampm = (found["ampm"] or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return text, None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59 or second > 59:
        return text, None
    return found["date"], f"{hour:02d}:{minute:02d}:{second:02d}"


def dates_to_iso(date_texts: list, date_format: str) -> list:
    """Read each text with one layout from DATE_FORMATS. Returns "yyyy-mm-dd" strings, with
    None where a text does not fit the layout or is not a real date (February 30)."""
    if not date_texts:
        return []
    parsed = pd.to_datetime(pd.Index(date_texts, dtype=object), format=date_format, errors="coerce")
    iso = parsed.strftime("%Y-%m-%d")
    return [day if isinstance(day, str) else None for day in iso.tolist()]


def excel_serial_to_iso(serial: float, with_time: bool):
    """Excel stores a date as the number of days since 1899-12-30; the fraction is the time."""
    moment = pd.Timestamp("1899-12-30") + pd.to_timedelta(round(serial * 86400), unit="s")
    return moment.strftime("%Y-%m-%d %H:%M:%S" if with_time else "%Y-%m-%d")


def yyyymmdd_to_iso(number: float):
    """20240105 -> "2024-01-05"; None when the digits are not a real date."""
    text = str(int(number))
    result = dates_to_iso([f"{text[:4]}-{text[4:6]}-{text[6:8]}"], "%Y-%m-%d")
    return result[0]
