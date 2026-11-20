"""
Cleaning of single pieces of text that came out of an uploaded file.

Everything in an uploaded file was typed by a stranger, so every cell, every header, every
sheet name and the file name itself pass through this module before anything else looks at
them. The rules are plain code (no model is involved):

  * invisible and control characters are removed and whitespace is squeezed,
  * "N/A", "#DIV/0!" and similar markers become empty cells,
  * text that looks like an instruction to an AI model, or like a link, is replaced by a
    fixed marker so it can never reach a prompt,
  * text is cut to 200 characters.

The redaction is pattern based. It removes the common shapes of prompt injection and of
data-stealing links; it cannot recognise every possible wording. The rest of the system must
therefore still treat file text as data and never as instructions.
"""
from __future__ import annotations

import re
import unicodedata

MAX_TEXT = 200          # longest text kept from one cell
MAX_SCAN = 2000         # how much of a long cell is checked for instructions and links
MAX_NAME = 40           # longest table or column name

REMOVED_INSTRUCTION = "[removed: instruction-like text]"
REMOVED_LINK = "[removed: link]"

NO_NOTES: tuple = ()

# Cells that mean "no value". "NA" and "None" are deliberately NOT in this list: they can be
# real values (a region called NA, a discount type called None).
NULL_MARKERS = frozenset({
    "n/a", "#n/a", "n.a.", "null", "nan", "<na>", "nil", "-", "--", "---", "?", "??",
    "#value!", "#div/0!", "#ref!", "#name?", "#null!", "#num!", "(blank)", "(null)",
})

# Control characters other than tab / line feed / carriage return, plus the invisible
# "format" characters (zero-width spaces, direction overrides, byte-order mark) that can be
# used to hide text from a pattern check.
_INVISIBLE = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f"
    "­​-‏‪-‮⁠-⁤﻿]"
)

# A value made only of digits and number punctuation cannot be an instruction or a link,
# so the pattern checks are skipped for it. This keeps number columns fast.
_NUMBER_LIKE = re.compile(r"[\d\s.,:/+\-$%()]*\Z")

# --- text that looks like an instruction to a model -------------------------------------
# These are matched against a simplified copy of the cell: lower case, with every run of
# punctuation or underscores turned into one space. "Ignore_previous-instructions!" is
# therefore checked as "ignore previous instructions".
_INSTRUCTION_WORDS = re.compile("|".join([
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instructions?|prompts?|rules|guidelines|"
    r"directions|context|above|previous|prior|earlier)\b",
    r"\bsystem (prompt|message|instructions?)\b",
    r"\bdeveloper (prompt|message|mode)\b",
    r"\byou are now\b",
    r"\byou must (now|always|never)\b",
    r"\bfrom now on\b",
    r"\bnew instructions?\b",
    r"\b(act|behave|respond) as (if|though|a|an|the)\b",
    r"\bpretend (to be|you|that)\b",
    r"\b(reveal|print|show|repeat|leak|send)\b.{0,30}\b(prompt|instructions|secrets?|api key|password)s?\b",
    r"\bdo not (tell|inform|mention|reveal)\b",
    r"\b(call|use|run|invoke|execute) (the )?(tool|function|command)\b",
    r"\bjailbreak\b",
    r"\b(as an ai|ai assistant|language model)\b",
]))

# These are matched against the cell as written, because the punctuation is the signal:
# chat-template tokens and role prefixes.
_INSTRUCTION_MARKUP = re.compile("|".join([
    r"<\|[a-z_]{1,30}\|>",                       # <|im_start|> and friends
    r"\[/?INST\]",                               # [INST] ... [/INST]
    r"<<\s*/?SYS\s*>>",                          # <<SYS>>
    r"^\s*(system|assistant|developer)\s*:",     # "system: ..." at the start of a cell
    r"#{2,}\s*(instruction|system|response)",    # "### Instruction"
]), re.IGNORECASE)

# --- text that looks like a link or like markup that can carry one -----------------------
_LINK = re.compile("|".join([
    r"[a-z][a-z0-9+.\-]{1,15}://",                      # http://  https://  ftp://  file://
    r"\bwww\.[a-z0-9\-]+\.[a-z]{2,}",                   # www.example.com
    r"\b(mailto|javascript|vbscript)\s*:",              # mailto:  javascript:
    r"\bdata:[a-z]+/[a-z0-9.+\-]+[;,]",                 # data:image/png;base64,...
    r"!?\[[^\]]{0,200}\]\s*\([^)]{0,500}\)",            # markdown link or image
    r"<\s*/?\s*[a-z!][^<>]{0,300}>",                    # an HTML tag
]), re.IGNORECASE)

_PUNCTUATION_RUN = re.compile(r"[\W_]+")


def looks_like_instruction(text: str) -> bool:
    """True when the text has the shape of a message aimed at an AI model."""
    if _INSTRUCTION_MARKUP.search(text):
        return True
    simplified = _PUNCTUATION_RUN.sub(" ", text.lower())
    return _INSTRUCTION_WORDS.search(simplified) is not None


def looks_like_link(text: str) -> bool:
    """True when the text holds a link, a markdown link or image, or an HTML tag."""
    return _LINK.search(text) is not None


def clean_cell(value) -> tuple:
    """Clean one raw cell.

    Returns (text, notes). `text` is the cleaned string, or None for an empty cell. `notes`
    is a tuple naming what was done, so the caller can count each action for the report:
    "control_characters", "whitespace", "null_marker", "instruction", "link", "cut".
    """
    if value is None:
        return None, NO_NOTES
    text = value if isinstance(value, str) else str(value)
    notes = []

    # 1. Unicode. NFKC folds look-alike characters (full-width letters, ligatures) to their
    #    plain form, so "ｉｇｎｏｒｅ" is checked as "ignore". Then invisible characters go.
    if not (text.isascii() and text.isprintable()):
        folded = unicodedata.normalize("NFKC", text)
        visible = _INVISIBLE.sub("", folded)
        if visible != folded:
            notes.append("control_characters")
        text = visible

    # 2. Whitespace. split() with no argument splits on every kind of whitespace, so tabs
    #    and line breaks inside a cell become single spaces and the ends are trimmed.
    squeezed = " ".join(text.split())
    if squeezed != text:
        if not squeezed:
            return None, NO_NOTES            # the cell held only spaces: it is simply empty
        notes.append("whitespace")
        text = squeezed
    if not text:
        return None, NO_NOTES

    # 3. Markers that mean "no value".
    if len(text) <= 8 and text.lower() in NULL_MARKERS:
        return None, ("null_marker",)

    # 4. Instruction-like text and links: the whole cell is replaced, never repaired.
    if not _NUMBER_LIKE.match(text):
        scanned = text[:MAX_SCAN]
        if looks_like_instruction(scanned):
            return REMOVED_INSTRUCTION, ("instruction",)
        if looks_like_link(scanned):
            return REMOVED_LINK, ("link",)

    # 5. Length.
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rstrip()
        notes.append("cut")
    return text, tuple(notes) if notes else NO_NOTES


def clean_label(value, limit: int = 80) -> str:
    """Clean a short piece of text that will be shown or stored as a label: a file name,
    a sheet name or an original column header. Never returns None."""
    text, _notes = clean_cell(value)
    return (text or "")[:limit]


def display_file_name(name) -> str:
    """The file name to store and show: no folders, cleaned like any other untrusted text.
    The full path of an upload is never stored, so it can never reach a model."""
    text = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    # The ending (.xlsx, .csv) is kept apart so that a name which gets replaced as
    # instruction-like text still says what type of file it is.
    stem, dot, ending = text.rpartition(".")
    if not dot or not re.fullmatch(r"[A-Za-z0-9]{1,5}", ending):
        stem, ending = text, ""
    label = clean_label(stem) or "file"
    return f"{label}.{ending}" if ending else label


# --- names for tables and columns -------------------------------------------------------
_LOWER_THEN_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")          # unitPrice  -> unit_Price
_UPPER_RUN_THEN_WORD = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")     # SKUCode    -> SKU_Code
_NOT_NAME_CHARACTER = re.compile(r"[^a-z0-9]+")

# Words SQLite treats specially. A column called "order" or "group" would have to be quoted
# in every query, so such names get the ending "_col" instead.
SQL_KEYWORDS = frozenset("""
abort action add after all alter always analyze and as asc attach autoincrement before begin
between by cascade case cast check collate column commit conflict constraint create cross
current current_date current_time current_timestamp database default deferrable deferred
delete desc detach distinct do drop each else end escape except exclude exclusive exists
explain fail filter first following for foreign from full generated glob group groups having
if ignore immediate in index indexed initially inner insert instead intersect into is isnull
join key last left like limit match materialized natural no not nothing notnull null nulls of
offset on or order others outer over partition plan pragma preceding primary query raise
range recursive references regexp reindex release rename replace restrict returning right
rollback row rows savepoint select set table temp temporary then ties to transaction trigger
unbounded union unique update using vacuum values view virtual when where window with without
rowid oid
""".split())


def normalise_header(text) -> str:
    """Turn a header such as "Unit Price ($)" into a snake_case name such as "unit_price".

    Returns "" when nothing usable is left (for example a header written only in symbols).
    The result has only a-z, 0-9 and underscore, starts with a letter and is at most 40 long.
    """
    if not text:
        return ""
    # Drop accents: "Précio" -> "Precio".
    decomposed = unicodedata.normalize("NFKD", str(text))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Symbols that carry meaning in a header become words.
    plain = plain.replace("%", " pct ").replace("#", " no ").replace("&", " and ")
    # Split camelCase before lower-casing, so "InvoiceNo" becomes "invoice_no".
    plain = _LOWER_THEN_UPPER.sub("_", plain)
    plain = _UPPER_RUN_THEN_WORD.sub("_", plain)
    name = _NOT_NAME_CHARACTER.sub("_", plain.lower()).strip("_")
    if not name:
        return ""
    if not name[0].isalpha():
        name = "c_" + name               # a name must start with a letter
    return name[:MAX_NAME].rstrip("_")


def unique_names(wanted: list) -> list:
    """Make a list of column names safe and unique.

    `wanted` holds normalised headers; an empty entry means the column had no usable header
    and is named by its position ("column_3"). SQL keywords get "_col"; repeats get "_2",
    "_3", ... Every result still fits in 40 characters.
    """
    taken = set()
    result = []
    for position, name in enumerate(wanted, start=1):
        base = name or f"column_{position}"
        if base in SQL_KEYWORDS:
            base = base + "_col"
        candidate = base
        number = 2
        while candidate in taken:
            suffix = f"_{number}"
            candidate = base[:MAX_NAME - len(suffix)] + suffix
            number += 1
        taken.add(candidate)
        result.append(candidate)
    return result


def table_name_for(label, taken: set) -> str:
    """A unique table name "t_<slug>" built from a sheet name or a file name."""
    slug = normalise_header(label) or "table"
    base = ("t_" + slug)[:MAX_NAME].rstrip("_")
    candidate = base
    number = 2
    while candidate in taken:
        suffix = f"_{number}"
        candidate = base[:MAX_NAME - len(suffix)] + suffix
        number += 1
    taken.add(candidate)
    return candidate
