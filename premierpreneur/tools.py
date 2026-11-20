"""
Deterministic calculation tools.

Design rule from the proposal: the language model never computes a number.
Every margin, coverage figure and scenario cost comes from these Python
functions over SQLite. The model's job is to choose which tool to call,
with what arguments, and to interpret the validated results.

Every tool:
  * validates its inputs and raises ToolError with a readable message
    (the agent loop hands that message back to the model as an observation
    so it can correct itself instead of crashing),
  * returns plain JSON-serialisable dicts,
  * reports data gaps explicitly (e.g. products with no unit cost) instead
    of silently dropping or inventing values.

Milestone 3 changes (docs/M3_BUILD_SPEC.md, sections 3 and 6):
  * Any dataset. A database written by the ingestion step carries a
    _capabilities table that says which analyses its data supports. Each tool
    checks it first and, when its capability is off, says in plain words what
    is missing. The built-in store (built by db.py) has no such table and
    behaves as before.
  * Missing values. Prices, costs, suppliers and lead times may be NULL and
    any table may be empty. A missing value is reported as a gap; a lead time
    or minimum order taken from the owner's defaults (_assumptions) is marked
    as assumed.
  * Big data. The heavy work (adding up sales lines) is done in SQL. Every
    result is bounded: the top rows, store-wide totals and a count of what is
    not shown, so a result fits in the model's 8,192-token context.
  * Four new tools: describe_data, query_data, compare_scenarios,
    compare_periods.

The model never writes SQL. query_data builds its SQL from a fixed set of
templates; table and column names must match the data dictionary exactly, and
every value is passed to SQLite as a bound parameter. The database connection
is opened read-only (PRAGMA query_only), so no tool can change the data.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from .db import DB_PATH

STRATEGY_TARGET_DAYS = {"conservative": 21, "moderate": 35, "aggressive": 56}
OVERSTOCK_DAYS = 120

# ---- limits that keep every result small ------------------------------------
TOP_N_DEFAULT = 20        # rows shown by default (the 12-product store fits whole)
TOP_N_MAX = 30            # the most rows any list tool will show
PLAN_LINES_MAX = 30       # order lines shown by plan_restock
SCENARIO_LINES_MAX = 10   # order lines shown per scenario by compare_scenarios
SCENARIO_DIFFS_MAX = 3    # products whose order quantity differs most between scenarios
SCENARIO_LEFT_OUT_MAX = 5 # compare_scenarios names left-out products only when there are this few
FULL_DETAIL_MAX = 12      # a list up to this long is returned whole and in full detail
EXAMPLES_MAX = 3          # a longer list of left-out products is cut to this many examples, with a count
LISTING_MAX = 12          # products named in one error message
GAP_LINES_MAX = 10        # per-product lines in one data_gaps list
NAME_MAX = 60             # product names are cut to this many characters
QUERY_TEXT_MAX = 80       # text cells in a query_data result are cut to this
QUERY_LIMIT_MAX = 50      # rows a query_data call may return
QUERY_SECONDS_MAX = 10    # a query_data call that runs longer is stopped
REASON_MAX = 200          # a capability reason read from the database is cut to this

SALES_SORT_KEYS = ("net_revenue", "units_sold", "gross_profit", "gross_margin_pct", "units_trend_pct")
INVENTORY_STATUSES = ("stockout_risk", "low", "lead_time_unknown", "ok", "overstock", "no_sales_last_30d")
STATUS_ORDER = {name: position for position, name in enumerate(INVENTORY_STATUSES)}


class ToolError(ValueError):
    """Raised for bad arguments or impossible requests. Message is model-facing."""


# --------------------------------------------------------------------------- #
# helpers: connection, numbers, text
# --------------------------------------------------------------------------- #
def _conn(db_path=None) -> sqlite3.Connection:
    """Open the dataset read-only. A missing file is an error; it is never created here."""
    path = Path(db_path or DB_PATH)
    if not path.is_file():
        raise ToolError("the dataset database was not found; load a dataset before asking about it.")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")          # tools only read; a write would fail
    return conn


@contextmanager
def _database(db_path=None):
    """`with _database(db_path) as conn:` opens the dataset, always closes it, and turns a
    database error into a ToolError so the agent loop gets a message instead of a crash."""
    conn = _conn(db_path)
    try:
        yield conn
    except sqlite3.Error as e:
        raise ToolError(f"the dataset could not be read ({str(e)[:120]}); reload the file and try again.")
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return row is not None


def _as_of(conn: sqlite3.Connection) -> date:
    """The last day with a sale. Every "last N days" window ends here, not at today's date."""
    row = conn.execute("SELECT MAX(sale_date) AS d FROM sales").fetchone()
    if row is None or row["d"] is None:
        raise ToolError("sales table is empty; upload sales data before analysing.")
    try:
        return date.fromisoformat(str(row["d"])[:10])      # a time of day, if any, is ignored
    except ValueError:
        raise ToolError("the sale dates are not in yyyy-mm-dd form; reload the file so its dates can be read.")


def _r2(x: float | None) -> float | None:
    """Round to cents. Adding 0.0 turns a rounded "-0.0" into a plain 0.0."""
    return None if x is None else round(float(x), 2) + 0.0


def _clean_text(value: Any, limit: int) -> str:
    """Text from the data, made safe to show: line breaks and other control characters become
    spaces, and the text is cut to `limit` characters."""
    text = str(value)
    if not text.isprintable():
        text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:limit]


def _name(value: Any) -> str | None:
    return None if value is None else _clean_text(value, NAME_MAX)


def _money_or_count(x: float) -> float:
    """Round money to cents; leave a whole-number count as it is."""
    return x if isinstance(x, int) else round(x, 2)


def _change(current: float, previous: float) -> dict:
    """current against previous, with the difference and the percent change."""
    return {
        "current": _money_or_count(current),
        "previous": _money_or_count(previous),
        "change": _money_or_count(current - previous),
        "change_pct": None if previous == 0 else _r2((current - previous) / previous * 100),
    }


# --------------------------------------------------------------------------- #
# helpers: argument checks
# --------------------------------------------------------------------------- #
def _check_int(name: str, value: Any, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer between {lo} and {hi}, got {value!r}")
    if not lo <= v <= hi:
        raise ToolError(f"{name} must be between {lo} and {hi}, got {v}")
    return v


def _check_top_n(name: str, value: Any, hi: int = TOP_N_MAX) -> int:
    """How many rows to show: a whole number from 1. Anything above `hi` is cut to `hi`,
    because the result always says how many rows were not shown."""
    if isinstance(value, bool):
        raise ToolError(f"{name} must be a whole number between 1 and {hi}, got {value!r}")
    try:
        v = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ToolError(f"{name} must be a whole number between 1 and {hi}, got {value!r}")
    if v < 1:
        raise ToolError(f"{name} must be between 1 and {hi}, got {v}")
    return min(v, hi)


def _check_choice(name: str, value: Any, choices: tuple) -> str:
    text = str(value).lower().strip()
    if text not in choices:
        raise ToolError(f"{name} must be one of {list(choices)}, got {value!r}")
    return text


def _check_budget(budget: Any) -> float:
    try:
        budget = float(budget)
    except (TypeError, ValueError):
        raise ToolError(f"budget must be a number, got {budget!r}")
    if not math.isfinite(budget):
        raise ToolError("budget must be an ordinary number")
    if budget <= 0:
        raise ToolError(f"budget must be greater than 0, got {budget}")
    return budget


def _check_product_list(product_ids: Any) -> list[str] | None:
    """product_ids is optional: None, or a list of ids / names (numbers are read as ids)."""
    if product_ids is None:
        return None
    ok = isinstance(product_ids, list) and all(
        isinstance(x, (str, int)) and not isinstance(x, bool) for x in product_ids)
    if not ok:
        raise ToolError("product_ids must be a list of product ids or names, e.g. ['P001','French Press']")
    if len(product_ids) > TOP_N_MAX:
        raise ToolError(f"product_ids can hold at most {TOP_N_MAX} products, got {len(product_ids)}")
    return [str(x) for x in product_ids]


# --------------------------------------------------------------------------- #
# helpers: capabilities and owner defaults (section 3 of the build spec)
# --------------------------------------------------------------------------- #
# What the owner is told when a capability is off: what is missing, then what to add.
CAPABILITY_MESSAGES = {
    "sales_analysis": "This dataset has no usable sales lines (a product, a date and a quantity for each "
                      "sale), so I cannot analyse sales. Add a sheet with one row per sale.",
    "revenue": "This dataset has no prices for its sales lines, so I cannot work out revenue. "
               "Add a unit price or a line total column to the sales data.",
    "margin": "This dataset has no unit costs, so I cannot work out profit or margin. "
              "Add a column with the unit cost of each product.",
    "inventory_status": "This dataset has no stock levels, so I cannot say what is running low. "
                        "Add a column with units on hand for each product.",
    "supplier_terms": "This dataset has no supplier lead times, so I cannot report supplier terms. "
                      "Add a lead time (days) column, or set a default lead time for the dataset.",
    "restock_planning": "This dataset is missing what a restock plan needs: units on hand and a unit "
                        "cost for the products. Add both columns.",
    "discount_evaluation": "This dataset is missing what a discount check needs: a price and a unit cost "
                           "for the product. Add both columns.",
}
# A capability also needs these to be on. They are checked first, so the message names the real gap.
CAPABILITY_NEEDS = {
    "sales_analysis": [],
    "revenue": ["sales_analysis"],
    "margin": ["sales_analysis", "revenue"],
    "inventory_status": ["sales_analysis"],
    "supplier_terms": [],
    "restock_planning": ["sales_analysis", "inventory_status"],
    "discount_evaluation": ["sales_analysis", "revenue", "margin"],
}


def _load_capabilities(conn: sqlite3.Connection) -> dict | None:
    """{name: {"available": bool, "reason": str}} from the _capabilities table.
    None means the table does not exist: an older database, where nothing is switched off."""
    if not _has_table(conn, "_capabilities"):
        return None
    rows = conn.execute("SELECT name, available, reason FROM _capabilities").fetchall()
    return {str(r["name"]): {"available": bool(r["available"]),
                             "reason": _clean_text(r["reason"] or "", REASON_MAX)} for r in rows}


def _is_on(caps: dict | None, name: str) -> bool:
    """A capability with no row counts as on; the tool's own checks for missing data still apply."""
    if caps is None or name not in caps:
        return True
    return caps[name]["available"]


def _require(caps: dict | None, name: str) -> None:
    """Stop with a readable message when the dataset cannot support this tool. The message is
    the fixed sentence above, so it always says what is missing and what to add; the reason
    stored in the database is shown by describe_data."""
    for needed in CAPABILITY_NEEDS[name] + [name]:
        if not _is_on(caps, needed):
            raise ToolError(CAPABILITY_MESSAGES[needed])


def _load_assumptions(conn: sqlite3.Connection) -> dict[str, str]:
    """Defaults the owner set for this dataset, e.g. {"default_lead_time_days": "14"}."""
    if not _has_table(conn, "_assumptions"):
        return {}
    rows = conn.execute("SELECT name, value FROM _assumptions").fetchall()
    return {str(r["name"]): _clean_text(r["value"], 40) for r in rows if r["value"] is not None}


def _assumed_int(assumptions: dict, name: str, lo: int, hi: int) -> int | None:
    """An owner default as a whole number, or None when it is not set or not sensible."""
    try:
        value = int(float(assumptions[name]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return value if lo <= value <= hi else None


def _moq_default(conn: sqlite3.Connection, assumptions: dict) -> int | None:
    """The owner's default minimum order, but only when the file itself had no MOQ column.
    (The products table always holds a number for moq, so the data dictionary is the only
    place that says whether that number came from the file.)"""
    default = _assumed_int(assumptions, "default_moq", 1, 1_000_000)
    if default is None or not _has_table(conn, "_columns"):
        return None
    in_file = conn.execute("SELECT 1 FROM _columns WHERE role = 'moq' LIMIT 1").fetchone()
    return None if in_file else default


# --------------------------------------------------------------------------- #
# helpers: products and sales
# --------------------------------------------------------------------------- #
def _catalog(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(product_id, product_name) for every product, in id order."""
    rows = conn.execute("SELECT product_id, product_name FROM products ORDER BY product_id").fetchall()
    return [(str(r["product_id"]), str(r["product_name"] or "")) for r in rows]


def _listing(pairs: list[tuple[str, str]]) -> str:
    """'P001 Name, P002 Name, ...' for an error message, never more than LISTING_MAX products."""
    shown = ", ".join(f"{pid} {_name(name)}" for pid, name in pairs[:LISTING_MAX])
    if len(pairs) > LISTING_MAX:
        shown += f" and {len(pairs) - LISTING_MAX} more"
    return shown


def _find_product(conn: sqlite3.Connection, text: Any, catalog: list | None = None) -> str:
    """Turn what the model passed (an id like 'P004' or a name like 'cast iron skillet')
    into a real product_id. Never guesses: no match or several matches is an error."""
    if isinstance(text, int) and not isinstance(text, bool):
        text = str(text)                                 # ids in real files are often plain numbers
    if not isinstance(text, str) or not text.strip():
        raise ToolError(f"a product must be a product id or product name, got {text!r}")
    wanted = text.strip().lower()
    products = _catalog(conn) if catalog is None else catalog
    if not products:
        raise ToolError("this dataset has no product list, so a product cannot be looked up.")
    for pid, name in products:                           # exact id first
        if pid.lower() == wanted:
            return pid
    exact = [p for p in products if p[1].lower() == wanted]
    if len(exact) == 1:                                  # the whole name, typed exactly
        return exact[0][0]
    matches = [p for p in products if wanted in p[1].lower()]
    if len(matches) == 1:
        return matches[0][0]
    if matches:
        raise ToolError(f"{text!r} matches more than one product: {_listing(matches)}. Use the product id.")
    if len(products) <= LISTING_MAX:
        raise ToolError(f"no product has the id or name {text!r}. The products are: {_listing(products)}")
    raise ToolError(f"no product has the id or name {text!r}. There are {len(products)} products, for example: "
                    f"{_listing(products)}. To search, call query_data on table 'products' with a "
                    f"'contains' filter on product_name.")


def _units_by_product(conn: sqlite3.Connection, start: date, end: date) -> dict[str, dict]:
    """Per product, for sales after `start` up to and including `end`:
         units           all units sold
         revenue         net of discount, over the sales lines that have a price
         unpriced_units  units on lines with no price; they are in `units` but not in `revenue`
    The date test uses whole days, so it is right for dates with or without a time of day.

    Speed: SQLite would use the date index for any date range, which means one separate row
    lookup per sale. Measured on a million rows, that only pays off for a window shorter than
    about a twentieth of the sales history; for anything wider, reading the table straight
    through once is faster, so the query says NOT INDEXED."""
    first_day = (start + timedelta(days=1)).isoformat()
    day_after_end = (end + timedelta(days=1)).isoformat()
    history = conn.execute("SELECT MIN(sale_date) AS first, MAX(sale_date) AS last FROM sales").fetchone()
    wide = True
    if history["first"] is not None:
        try:
            history_days = (date.fromisoformat(str(history["last"])[:10])
                            - date.fromisoformat(str(history["first"])[:10])).days + 1
            wide = (end - start).days * 20 >= history_days       # a twentieth of the history or more
        except ValueError:                               # dates that cannot be read: do not rely on the index
            wide = True
    rows = conn.execute(
        f"""
        SELECT s.product_id,
               SUM(s.quantity)                                                   AS units,
               SUM(s.quantity * s.unit_price_at_sale
                   * (1 - COALESCE(s.discount_pct, 0)/100.0))                    AS revenue,
               SUM(CASE WHEN s.unit_price_at_sale IS NULL THEN s.quantity ELSE 0 END) AS unpriced_units
        FROM sales s {"NOT INDEXED" if wide else ""}
        WHERE s.sale_date >= ? AND s.sale_date < ?
        GROUP BY s.product_id
        """,
        (first_day, day_after_end),
    ).fetchall()
    return {str(r["product_id"]): dict(r) for r in rows}


def _figures(sold: dict | None, unit_cost: float | None) -> tuple[int, float | None, float | None, float | None]:
    """(units, net revenue, COGS, gross profit) for one product in one period.
    `sold` is that product's row from _units_by_product, or None when nothing was sold.
    COGS is the cost of the units that were sold with a price, so it matches the revenue.
    Anything that cannot be known is None: revenue when no sold line has a price, COGS and
    gross profit when the product has no unit cost."""
    sold = sold or {}
    units = int(sold.get("units") or 0)
    unpriced = int(sold.get("unpriced_units") or 0)
    if units > 0 and unpriced >= units:
        return units, None, None, None
    revenue = float(sold.get("revenue") or 0.0)
    cogs = None if unit_cost is None else (units - unpriced) * float(unit_cost)
    gross_profit = None if cogs is None else revenue - cogs
    return units, revenue, cogs, gross_profit


def _gap_lines(ids: list[str], text: str, shown: int = 0) -> list[str]:
    """One 'P011: <text>' line per product, at most GAP_LINES_MAX, then a count of the rest.
    When the gap affects every one of the `shown` products, it is said once instead."""
    if len(ids) == shown > 1:
        return [f"all {shown} products shown here: {text}"]
    lines = [f"{pid}: {text}" for pid in ids[:GAP_LINES_MAX]]
    if len(ids) > GAP_LINES_MAX:
        lines.append(f"{len(ids) - GAP_LINES_MAX} more products shown here: {text}")
    return lines


# --------------------------------------------------------------------------- #
# tool 1: sales summary
# --------------------------------------------------------------------------- #
def _sales_items(rows: list[tuple], cur: dict, prev: dict, days: int) -> tuple[list[dict], dict]:
    """One summary dict per product, in the order given, plus the store-wide totals.
    `rows` holds (product_id, product_name, unit_cost); `cur` and `prev` come from _units_by_product."""
    items = []
    totals = {"units": 0.0, "revenue": 0.0, "profit": 0.0, "unpriced_units": 0, "missing_cost": []}
    for pid, name, unit_cost in rows:
        units, revenue, cogs, gp = _figures(cur.get(pid), unit_cost)
        margin_pct = None if gp is None or revenue == 0 else gp / revenue * 100
        prev_units = int(prev.get(pid, {}).get("units", 0) or 0)
        trend = None if prev_units == 0 else (units - prev_units) / prev_units * 100
        if unit_cost is None:
            totals["missing_cost"].append(pid)
        items.append({
            "product_id": pid,
            "product_name": _name(name),
            "units_sold": units,
            "avg_daily_units": _r2(units / days),
            "net_revenue": _r2(revenue),
            "cogs": _r2(cogs),
            "gross_profit": _r2(gp),
            "gross_margin_pct": _r2(margin_pct),
            "units_prev_period": prev_units,
            "units_trend_pct": _r2(trend),
        })
        totals["units"] += units
        totals["revenue"] += revenue or 0.0
        totals["profit"] += gp or 0.0
        totals["unpriced_units"] += int((cur.get(pid) or {}).get("unpriced_units") or 0)
    return items, totals


def _sales_gaps(shown: list[dict], totals: dict, revenue_on: bool, margin_on: bool, not_in_catalog: list) -> list[str]:
    """What the sales summary could not work out, in plain sentences. A gap that affects the
    whole dataset is one sentence, not one line per product."""
    gaps = []
    if not revenue_on:
        gaps.append(CAPABILITY_MESSAGES["revenue"])
    elif totals["unpriced_units"]:
        gaps.append(f"{totals['unpriced_units']} units sold in this period have no price on file; "
                    f"net revenue and gross profit leave them out")
    if revenue_on and not margin_on:
        gaps.append(CAPABILITY_MESSAGES["margin"])
    elif revenue_on:
        shown_ids = {i["product_id"] for i in shown}
        gaps += _gap_lines([pid for pid in totals["missing_cost"] if pid in shown_ids],
                           "unit_cost missing - COGS, gross profit and margin cannot be computed", len(shown))
        hidden = len([pid for pid in totals["missing_cost"] if pid not in shown_ids])
        if hidden:
            gaps.append(f"{hidden} products not shown here also have no unit cost")
    if not_in_catalog:
        gaps.append(f"{len(not_in_catalog)} products sold in this period are not in the product list; "
                    f"their names and costs are unknown")
    return gaps


def get_sales_summary(days: int = 30, top_n: int = TOP_N_DEFAULT, sort_by: str = "net_revenue",
                      product_ids: list[str] | None = None, db_path=None) -> dict:
    """Per-product units, net revenue, COGS, gross profit and margin for the
    last `days` days, plus the same window immediately before it for trend.

    Totals cover the whole store. `products` holds the `top_n` products ranked by
    `sort_by` (highest first), or exactly the products named in `product_ids`.
    `products_not_shown` counts the products that were ranked but did not fit in the list."""
    days = _check_int("days", days, 1, 365)
    top_n = _check_top_n("top_n", top_n)
    sort_by = _check_choice("sort_by", sort_by, SALES_SORT_KEYS)
    wanted = _check_product_list(product_ids)
    with _database(db_path) as conn:
        caps = _load_capabilities(conn)
        _require(caps, "sales_analysis")
        end = _as_of(conn)
        start = end - timedelta(days=days)
        prev_start = start - timedelta(days=days)
        cur = _units_by_product(conn, start, end)
        prev = _units_by_product(conn, prev_start, start)
        products = conn.execute(
            "SELECT product_id, product_name, unit_cost FROM products ORDER BY product_id").fetchall()
        if wanted:                                       # ids or names -> real ids
            catalog = [(str(p["product_id"]), str(p["product_name"] or "")) for p in products]
            wanted = list(dict.fromkeys(_find_product(conn, x, catalog) for x in wanted))

    revenue_on, margin_on = _is_on(caps, "revenue"), _is_on(caps, "margin")
    rows = [(str(p["product_id"]), p["product_name"], p["unit_cost"]) for p in products]
    known = {pid for pid, _, _ in rows}
    # products that were sold but are not in the product list still count toward the totals
    not_in_catalog = sorted(pid for pid in cur if pid not in known)
    rows += [(pid, pid, None) for pid in not_in_catalog]
    items, totals = _sales_items(rows, cur, prev, days)

    # ---- choose what to show: fall back when the dataset cannot rank by the key asked for ----
    notes = []
    if sort_by in ("net_revenue", "gross_profit", "gross_margin_pct") and not revenue_on:
        notes.append(f"sorted by units_sold because {sort_by} is not available in this dataset")
        sort_by = "units_sold"
    elif sort_by in ("gross_profit", "gross_margin_pct") and not margin_on:
        notes.append(f"sorted by net_revenue because {sort_by} is not available in this dataset")
        sort_by = "net_revenue"
    matching = [i for i in items if i["product_id"] in wanted] if wanted else items
    # highest first; products with no value for the sort key go last (the sort is stable,
    # so equal values stay in product id order)
    matching = sorted(matching, key=lambda i: (i[sort_by] is None, -(i[sort_by] or 0)))
    shown = matching if wanted else matching[:top_n]

    return {
        "window_days": days,
        "period": {"start": (start + timedelta(days=1)).isoformat(), "end": end.isoformat()},
        "totals": {
            "units_sold": int(totals["units"]),
            "net_revenue": _r2(totals["revenue"]) if revenue_on else None,
            "gross_profit_known_cost_only": _r2(totals["profit"]) if (revenue_on and margin_on) else None,
        },
        "sorted_by": sort_by,
        "product_count": len(items),                     # every product in the store
        "products_not_shown": len(matching) - len(shown),
        "products": shown,
        "data_gaps": notes + _sales_gaps(shown, totals, revenue_on, margin_on, not_in_catalog),
    }


# --------------------------------------------------------------------------- #
# tool 2: inventory status
# --------------------------------------------------------------------------- #
def _stock_status(cover: float | None, lead: int | None) -> str:
    """The status of one product from its days of cover and its supplier lead time (None = unknown)."""
    if cover is None:
        return "no_sales_last_30d"
    if lead is None:
        # cannot tell whether stock lasts until a reorder arrives; only overstock is certain
        return "overstock" if cover > OVERSTOCK_DAYS else "lead_time_unknown"
    if cover < lead:
        return "stockout_risk"                 # will run out before a new order could arrive
    if cover < lead + 7:
        return "low"
    if cover > OVERSTOCK_DAYS:
        return "overstock"
    return "ok"


def _stock_position(conn: sqlite3.Connection, end: date) -> tuple[list[dict], dict[str, dict], list[str]]:
    """Every stocked product with its sales velocity, days of cover and status.

    Returns (items, extras, notes):
      items   one dict per product in the shape get_inventory_status shows, most urgent first
      extras  {product_id: unit_cost, moq, unit_margin, ...}: what plan_restock also needs
      notes   plain sentences about defaults that were used

    Lead time comes from the product's supplier. When that is missing and the owner has set a
    default lead time, the default is used and the product is marked as assumed. With neither,
    the lead time is unknown: the status says so and no number is invented."""
    assumptions = _load_assumptions(conn)
    lead_default = _assumed_int(assumptions, "default_lead_time_days", 0, 365)
    moq_default = _moq_default(conn, assumptions)
    cur = _units_by_product(conn, end - timedelta(days=30), end)
    rows = conn.execute(
        """
        SELECT i.product_id, p.product_id AS in_catalog, p.product_name, i.on_hand, i.reorder_point,
               p.unit_cost, p.moq, s.lead_time_days
        FROM inventory i
        LEFT JOIN products p ON p.product_id = i.product_id
        LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
        ORDER BY i.product_id
        """
    ).fetchall()

    items, extras = [], {}
    lead_assumed_count = 0
    for r in rows:
        pid = str(r["product_id"])
        units30, _, _, gross_profit = _figures(cur.get(pid), r["unit_cost"])
        velocity = units30 / 30.0
        lead, lead_assumed = r["lead_time_days"], False
        if lead is None and lead_default is not None:
            lead, lead_assumed = lead_default, True
            lead_assumed_count += 1
        lead = None if lead is None else int(lead)
        cover = None if velocity == 0 else r["on_hand"] / velocity
        status = _stock_status(cover, lead)
        item = {
            "product_id": pid,
            "product_name": _name(r["product_name"]) if r["in_catalog"] is not None else pid,
            "on_hand": int(r["on_hand"]),
            "reorder_point": int(r["reorder_point"] or 0),
            "below_reorder_point": bool(r["on_hand"] < (r["reorder_point"] or 0)),
            "avg_daily_units_30d": _r2(velocity),
            "days_of_cover": None if cover is None else round(cover, 1),
            "supplier_lead_time_days": lead,
            "status": status,
        }
        if lead_assumed:
            item["lead_time_assumed"] = True
        items.append(item)
        moq = int(r["moq"] or 1) if moq_default is None else moq_default
        extras[pid] = {
            "unit_cost": _r2(r["unit_cost"]),
            "moq": moq,
            "moq_assumed": moq_default is not None,
            "lead_time_assumed": lead_assumed,
            "in_catalog": r["in_catalog"] is not None,
            # gross profit per unit sold in the last 30 days; ranks products that are equally urgent
            "unit_margin": (_r2(gross_profit) or 0.0) / max(units30, 1),
        }
    items.sort(key=lambda x: (STATUS_ORDER[x["status"]], x["days_of_cover"] if x["days_of_cover"] is not None else 1e9))

    notes = []
    if lead_assumed_count:
        notes.append(f"lead time of {lead_default} days assumed for {lead_assumed_count} products with no "
                     f"supplier lead time on file (the owner's default)")
    if moq_default is not None and rows:
        notes.append(f"minimum order quantity of {moq_default} assumed for every product "
                     f"(the data has none; the owner's default)")
    return items, extras, notes


def get_inventory_status(status: str | None = None, top_n: int = TOP_N_DEFAULT, db_path=None) -> dict:
    """On-hand, velocity, days of cover and a stock status per product.
    Velocity is average daily units over the last 30 days of sales.

    `status_counts` covers every stocked product. `products` holds the `top_n` most urgent
    ones, or the `top_n` most urgent with the given `status`."""
    if status is not None:
        status = _check_choice("status", status, INVENTORY_STATUSES)
    top_n = _check_top_n("top_n", top_n)
    with _database(db_path) as conn:
        caps = _load_capabilities(conn)
        _require(caps, "inventory_status")
        end = _as_of(conn)
        items, extras, notes = _stock_position(conn, end)

    counts = {name: 0 for name in INVENTORY_STATUSES if name != "lead_time_unknown"}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    matching = items if status is None else [i for i in items if i["status"] == status]
    shown = matching[:top_n]

    definitions = {
        "stockout_risk": "days_of_cover < supplier lead time",
        "low": "days_of_cover < lead time + 7",
        "overstock": f"days_of_cover > {OVERSTOCK_DAYS}",
    }
    gaps = []
    unknown_lead = counts.get("lead_time_unknown", 0)
    if unknown_lead:
        definitions["lead_time_unknown"] = "no supplier lead time on file, so stockout risk cannot be judged"
        gaps.append(f"{unknown_lead} products have no supplier lead time, so I cannot say whether they will "
                    f"run out before a reorder arrives. Add lead times or set a default lead time.")
    orphans = len([1 for e in extras.values() if not e["in_catalog"]])
    if orphans:
        gaps.append(f"{orphans} stocked products are not in the product list; their names are unknown")
    result = {
        "as_of": end.isoformat(),
        "status_definitions": definitions,
        "status_counts": counts,
        "product_count": len(items),                     # every stocked product
        "products_not_shown": len(matching) - len(shown),   # with a status filter: more of that status
        "products": shown,
        "data_gaps": gaps,
    }
    if status is not None:
        result["status_filter"] = status
    if notes:
        result["assumptions"] = notes
    return result


# --------------------------------------------------------------------------- #
# tool 3: supplier terms
# --------------------------------------------------------------------------- #
def get_supplier_terms(product_ids: list[str] | None = None, top_n: int = TOP_N_DEFAULT, db_path=None) -> dict:
    """Unit cost, MOQ, supplier and lead time per product.

    With `product_ids`: exactly those products. Without: the first `top_n` products in id
    order, with a count of the rest."""
    wanted = _check_product_list(product_ids)
    top_n = _check_top_n("top_n", top_n)
    with _database(db_path) as conn:
        caps = _load_capabilities(conn)
        _require(caps, "supplier_terms")
        assumptions = _load_assumptions(conn)
        lead_default = _assumed_int(assumptions, "default_lead_time_days", 0, 365)
        moq_default = _moq_default(conn, assumptions)
        sql = """
            SELECT p.product_id, p.product_name, p.unit_cost, p.moq,
                   s.supplier_id, s.supplier_name, s.lead_time_days, s.payment_terms
            FROM products p LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
        """
        if wanted:                                       # ids or names -> real ids, each once
            catalog = _catalog(conn)
            wanted = list(dict.fromkeys(_find_product(conn, x, catalog) for x in wanted))
            sql += " WHERE p.product_id IN (%s) ORDER BY p.product_id" % ",".join("?" * len(wanted))
            rows = conn.execute(sql, tuple(wanted)).fetchall()
        else:
            rows = conn.execute(sql + " ORDER BY p.product_id LIMIT ?", (top_n,)).fetchall()
        product_count = conn.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]

    if wanted and not rows:
        raise ToolError(f"none of the requested product_ids exist: {wanted}")
    items = []
    for r in rows:
        lead, lead_assumed = r["lead_time_days"], False
        if lead is None and lead_default is not None:
            lead, lead_assumed = lead_default, True
        item = {
            "product_id": str(r["product_id"]),
            "product_name": _name(r["product_name"]),
            "unit_cost": _r2(r["unit_cost"]),
            "moq": int(r["moq"] or 1) if moq_default is None else moq_default,
            "supplier_id": _name(r["supplier_id"]),
            "supplier_name": _name(r["supplier_name"]),
            "lead_time_days": lead,
            "payment_terms": _name(r["payment_terms"]),
        }
        if lead_assumed:
            item["lead_time_assumed"] = True
        if moq_default is not None:
            item["moq_assumed"] = True
        items.append(item)

    no_cost = [i["product_id"] for i in items if i["unit_cost"] is None]
    no_lead = [i["product_id"] for i in items if i["lead_time_days"] is None]
    gaps = (_gap_lines(no_cost, "unit_cost missing", len(items))
            + _gap_lines(no_lead, "supplier lead time unknown", len(items)))
    result = {
        "product_count": product_count,
        "products_not_shown": 0 if wanted else product_count - len(items),
        "products": items,
        "data_gaps": gaps,
    }
    notes = []
    if any(i.get("lead_time_assumed") for i in items):
        notes.append(f"lead time of {lead_default} days is the owner's default, used where the data has none")
    if moq_default is not None:
        notes.append(f"minimum order quantity of {moq_default} is the owner's default; the data has none")
    if notes:
        result["assumptions"] = notes
    return result


# --------------------------------------------------------------------------- #
# tool 4: restock plan
# --------------------------------------------------------------------------- #
def _restock_inputs(conn: sqlite3.Connection) -> dict:
    """Read once everything the allocation needs. compare_scenarios reuses it for all three runs."""
    end = _as_of(conn)
    items, extras, notes = _stock_position(conn, end)
    return {"items": items, "extras": extras, "notes": notes}


def _candidates(inputs: dict, strategy: str) -> dict:
    """Step 1 of the restock method, which does not depend on the budget: which products need
    stock under this strategy, how much, and in what order. Products with no unit cost or no
    lead time are excluded and reported, never guessed.

    Method (fully deterministic, two passes):
      1. target cover = supplier lead time + strategy target days
         (conservative 21 / moderate 35 / aggressive 56)
      2. products ranked by urgency (fewest days of cover first), tie-break
         on gross margin per unit
      3. pass 1 "urgent": for each product, buy enough to survive its own
         lead time (ceil(velocity * lead_time - on_hand), rounded up to MOQ)
      4. pass 2 "top-up": with what is left, raise each product toward the
         full target cover in the same priority order
      5. anything still short is reported as unfunded with the dollar gap
    Steps 3 and 4 are in _spend.
    """
    target_days = STRATEGY_TARGET_DAYS[strategy]
    candidates, skipped, no_cost, no_lead = [], [], [], []
    for item in inputs["items"]:
        pid = item["product_id"]
        extra = inputs["extras"][pid]
        who = {"product_id": pid, "product_name": item["product_name"]}
        if extra["unit_cost"] is None:
            no_cost.append({**who, "reason": "unit_cost missing - cannot price an order", "_status": item["status"]})
            continue
        if extra["unit_cost"] <= 0:
            no_cost.append({**who, "reason": "unit_cost is zero - cannot price an order", "_status": item["status"]})
            continue
        velocity = item["avg_daily_units_30d"] or 0.0
        if velocity == 0:
            skipped.append({**who, "reason": "no sales in last 30 days"})
            continue
        lead = item["supplier_lead_time_days"]
        if lead is None:
            no_lead.append({**who, "reason": "supplier lead time unknown - cannot size an order",
                            "_status": item["status"]})
            continue
        horizon = lead + target_days
        target_qty = math.ceil(velocity * horizon - item["on_hand"])
        if target_qty <= 0:
            skipped.append({**who, "reason": f"already covered: {item['days_of_cover']}d on hand >= {horizon}d target"})
            continue
        moq = extra["moq"]
        urgent_qty = max(0, math.ceil(velocity * lead - item["on_hand"]))
        if 0 < urgent_qty < moq:
            urgent_qty = moq
        target_qty = max(target_qty, moq)
        candidates.append({
            "product_id": pid, "product_name": item["product_name"],
            "on_hand": item["on_hand"], "velocity": velocity,
            "days_of_cover": item["days_of_cover"], "status": item["status"],
            "lead_time_days": lead, "horizon_days": horizon,
            "urgent_qty": urgent_qty, "target_qty": target_qty,
            "moq": moq, "unit_cost": extra["unit_cost"], "unit_margin": extra["unit_margin"],
            "lead_time_assumed": extra["lead_time_assumed"], "moq_assumed": extra["moq_assumed"],
            "ordered": 0,
        })

    # urgency first (fewest days of cover), then higher unit margin
    candidates.sort(key=lambda c: (c["days_of_cover"], -c["unit_margin"]))
    return {
        "strategy": strategy, "target_days": target_days,
        "candidates": candidates, "skipped": skipped, "no_cost": no_cost, "no_lead": no_lead,
        "notes": inputs["notes"],
    }


def _spend(candidates: list[dict], budget: float) -> float:
    """Steps 3 and 4: spend the budget over the candidates in two passes. Sets c["ordered"] on
    every candidate (starting again from zero) and returns the money left."""
    for c in candidates:
        c["ordered"] = 0
    remaining = budget

    def _buy(c: dict, wanted: int) -> int:
        """Buy up to `wanted` more units of c within the remaining budget. Returns units bought."""
        nonlocal remaining
        if wanted <= 0:
            return 0
        affordable = min(wanted, math.floor(remaining / c["unit_cost"] + 1e-9))
        # a first purchase must satisfy MOQ; a top-up on an existing line can be any size
        if c["ordered"] == 0 and affordable < c["moq"]:
            return 0
        if affordable <= 0:
            return 0
        c["ordered"] += affordable
        remaining -= affordable * c["unit_cost"]
        return affordable

    # pass 1: survive the lead time
    for c in candidates:
        _buy(c, c["urgent_qty"])
    # pass 2: top up toward the strategy's target cover
    for c in candidates:
        _buy(c, c["target_qty"] - c["ordered"])
    return remaining


def _allocate(inputs: dict, budget: float, strategy: str) -> dict:
    """The whole plan for one strategy and one budget, not yet cut for display."""
    full = _candidates(inputs, strategy)
    full["budget"] = budget
    full["remaining"] = _spend(full["candidates"], budget)
    return full


def _require_lead_times(inputs: dict, full: dict) -> None:
    """A plan cannot be sized when products were left out for a missing lead time and not one
    stocked product has a lead time at all. Say so, and say what to add."""
    any_known = any(i["supplier_lead_time_days"] is not None for i in inputs["items"])
    if full["no_lead"] and not any_known:
        raise ToolError("No product has a supplier lead time, so I cannot size a restock order. "
                        "Add a lead time (days) column, or set a default lead time for the dataset.")


def _order_line(c: dict, short: bool) -> dict:
    """One order line. The short form is used when a plan has many lines, to keep the result small."""
    line = {"product_id": c["product_id"], "product_name": c["product_name"]}
    if short:
        line.update({"order_qty": int(c["ordered"]), "line_cost": _r2(c["ordered"] * c["unit_cost"])})
    else:
        line.update({
            "status": c["status"],
            "on_hand": c["on_hand"],
            "days_of_cover_now": c["days_of_cover"],
            "order_qty": int(c["ordered"]),
            "of_target_qty": int(c["target_qty"]),
            "unit_cost": _r2(c["unit_cost"]),
            "line_cost": _r2(c["ordered"] * c["unit_cost"]),
            "days_of_cover_after_delivery": round((c["on_hand"] + c["ordered"]) / c["velocity"], 1),
            "note": ("full target quantity" if c["ordered"] >= c["target_qty"]
                     else "lead time covered + partial top-up" if c["ordered"] > c["urgent_qty"] > 0
                     else "covers lead time only" if c["ordered"] == c["urgent_qty"] > 0
                     else "partial - budget limited"),
        })
    if c["lead_time_assumed"]:
        line["lead_time_assumed"] = True
    if c["moq_assumed"]:
        line["moq_assumed"] = True
    return line


def _public(entries: list[dict], limit: int | None = None) -> list[dict]:
    """The first `limit` entries (all of them when limit is None), without internal fields,
    whose names start with "_". The full count is always reported next to a list that was cut."""
    kept = entries if limit is None else entries[:limit]
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in kept]


def _unfunded(candidates: list[dict]) -> list[dict]:
    """Products the plan leaves short of their target, in priority order."""
    entries = []
    for c in candidates:
        short = c["target_qty"] - c["ordered"]
        if short > 0:
            entries.append({
                "product_id": c["product_id"],
                "product_name": c["product_name"],
                "status": c["status"],
                "units_short_of_target": int(short),
                "additional_cost_to_fully_fund": _r2(short * c["unit_cost"]),
                "lead_time_covered": bool(c["ordered"] >= c["urgent_qty"]),
            })
    return entries


def plan_restock(budget: float, strategy: str = "moderate", db_path=None) -> dict:
    """Allocate a fixed purchasing budget across products (the method is described in _candidates).

    Totals and counts always cover the whole plan. What is listed depends on size:
      small plan (no list longer than FULL_DETAIL_MAX)  every list whole, order lines in full detail
      big plan                                          at most PLAN_LINES_MAX order lines in a short
                                                        form, most urgent first, and EXAMPLES_MAX
                                                        examples of the products left short or left out"""
    budget = _check_budget(budget)
    strategy = _check_choice("strategy", strategy, tuple(STRATEGY_TARGET_DAYS))
    with _database(db_path) as conn:
        caps = _load_capabilities(conn)
        _require(caps, "restock_planning")
        inputs = _restock_inputs(conn)
    full = _allocate(inputs, budget, strategy)
    _require_lead_times(inputs, full)
    candidates = full["candidates"]

    ordered = [c for c in candidates if c["ordered"] > 0]
    unfunded = _unfunded(candidates)
    skipped = full["skipped"]
    big = any(len(entries) > FULL_DETAIL_MAX
              for entries in (ordered, unfunded, skipped, full["no_cost"], full["no_lead"]))
    plan = [_order_line(c, short=big) for c in ordered[:PLAN_LINES_MAX]]
    hidden = ordered[PLAN_LINES_MAX:]
    examples = EXAMPLES_MAX if big else None             # None = the whole list
    no_sales = len([1 for s in skipped if s["reason"].startswith("no sales")])

    total_cost = budget - full["remaining"]
    full_plan_cost = sum(c["target_qty"] * c["unit_cost"] for c in candidates)
    gaps = []
    if full["no_cost"]:
        gaps.append(f"{len(full['no_cost'])} products are left out of the plan because they have no unit cost")
    if full["no_lead"]:
        gaps.append(f"{len(full['no_lead'])} products are left out of the plan because their supplier lead "
                    f"time is unknown; add lead times or set a default lead time")
    assumptions = [
        "demand = average daily units over the last 30 days, assumed constant",
        "cover target = supplier lead time + strategy target days",
        "pass 1 funds every product through its own lead time; pass 2 tops up toward target",
        "first order of a product must meet supplier MOQ; top-ups can be any size",
        "unit cost from the product catalog; shipping and duties not included",
        "ranking: fewest days of cover first, then highest unit margin",
    ] + full["notes"]
    result = {
        "strategy": strategy,
        "target_days_cover_beyond_lead_time": full["target_days"],
        "budget": _r2(budget),
        "total_cost": _r2(total_cost),
        "remaining_budget": _r2(full["remaining"]),
        "budget_needed_for_full_target_plan": _r2(full_plan_cost),
        "plan": plan,
        "unfunded": _public(unfunded, examples),
        "skipped": _public(skipped, 0 if big else None),   # thousands on a big store: counts only
        "excluded_missing_cost": _public(full["no_cost"], examples),
        "excluded_missing_lead_time": _public(full["no_lead"], examples),
        "counts": {
            "order_lines": len(ordered),
            "order_lines_not_shown": len(hidden),
            "units_ordered": int(sum(c["ordered"] for c in ordered)),
            "unfunded": len(unfunded),
            "skipped_no_sales": no_sales,
            "skipped_already_covered": len(skipped) - no_sales,
            "excluded_missing_cost": len(full["no_cost"]),
            "excluded_missing_lead_time": len(full["no_lead"]),
        },
        "unfunded_cost_total": _r2(sum(c["unit_cost"] * (c["target_qty"] - c["ordered"]) for c in candidates)),
        "data_gaps": gaps,
        "assumptions": assumptions,
    }
    if hidden:
        result["cost_of_order_lines_not_shown"] = _r2(sum(c["ordered"] * c["unit_cost"] for c in hidden))
    return result


# --------------------------------------------------------------------------- #
# tool 5: discount evaluation
# --------------------------------------------------------------------------- #
def evaluate_discount(product_id: str, discount_pct: float, db_path=None) -> dict:
    """Margin at a discount level and the volume needed to keep gross profit flat."""
    try:
        discount_pct = float(discount_pct)
    except (TypeError, ValueError):
        raise ToolError(f"discount_pct must be a number, got {discount_pct!r}")
    if not 0 < discount_pct < 100:
        raise ToolError(f"discount_pct must be between 0 and 100 (exclusive), got {discount_pct}")

    with _database(db_path) as conn:
        caps = _load_capabilities(conn)
        _require(caps, "discount_evaluation")
        product_id = _find_product(conn, product_id)     # accepts an id or a name
        p = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
        end = _as_of(conn)
        cur = _units_by_product(conn, end - timedelta(days=30), end).get(product_id, {"units": 0, "revenue": 0.0})
        inv = None
        if _has_table(conn, "inventory"):
            inv = conn.execute("SELECT on_hand FROM inventory WHERE product_id = ?", (product_id,)).fetchone()

    if p["unit_cost"] is None:
        raise ToolError(f"{product_id} has no unit_cost on file; margin at a discount cannot be computed")
    if p["unit_price"] is None or float(p["unit_price"]) <= 0:
        raise ToolError(f"{product_id} has no unit price on file; a discounted price cannot be computed")

    price, cost = float(p["unit_price"]), float(p["unit_cost"])
    units30 = int(cur["units"] or 0)
    new_price = price * (1 - discount_pct / 100)
    cur_margin, new_margin = price - cost, new_price - cost
    cur_gp = cur_margin * units30
    breakeven_units = None if new_margin <= 0 else math.ceil(cur_gp / new_margin)
    return {
        "product_id": product_id,
        "product_name": _name(p["product_name"]),
        "unit_price": _r2(price),
        "unit_cost": _r2(cost),
        "on_hand": int(inv["on_hand"]) if inv else None,
        "units_sold_last_30d": units30,
        "current": {"unit_margin": _r2(cur_margin), "margin_pct": _r2(cur_margin / price * 100),
                    "gross_profit_30d": _r2(cur_gp)},
        "at_discount": {"discount_pct": discount_pct, "new_price": _r2(new_price),
                        "unit_margin": _r2(new_margin), "margin_pct": _r2(new_margin / new_price * 100) if new_price else None,
                        "sells_below_cost": new_margin <= 0},
        "breakeven": {
            "units_needed_30d_to_hold_gross_profit": breakeven_units,
            "required_volume_lift_pct": None if breakeven_units is None or units30 == 0
                                       else _r2((breakeven_units - units30) / units30 * 100),
        },
        "assumptions": ["demand response to the discount is NOT modeled; breakeven shows the volume the owner "
                        "would need to believe in, not a prediction"],
    }


# --------------------------------------------------------------------------- #
# tool 6: describe the data
# --------------------------------------------------------------------------- #
CANONICAL_TABLES = ("sales", "products", "inventory", "suppliers")
CANONICAL_KINDS = {"sales": "transactions", "products": "products",
                   "inventory": "inventory", "suppliers": "suppliers"}
CANONICAL_DATE_COLUMNS = {("sales", "sale_date"), ("inventory", "last_counted")}
DTYPES = ("text", "number", "integer", "date", "bool")
NUMERIC_DTYPES = ("number", "integer", "bool")
ROLES = ("product_id", "product_name", "category", "sale_date", "quantity", "unit_price", "unit_cost",
         "revenue", "discount_pct", "channel", "order_id", "customer_id", "on_hand", "reorder_point",
         "supplier_id", "supplier_name", "lead_time_days", "moq", "payment_terms")
DESCRIBE_TABLES_MAX = 12
DESCRIBE_COLUMNS_MAX = 40          # per table
DESCRIBE_COLUMNS_TOTAL = 80        # across the whole result, so it fits the model's context

# A table or column name may only ever be: a lowercase letter, then lowercase letters, digits
# or underscores. Nothing that could end a name or start another SQL clause can match.
_SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _is_safe_name(name: Any) -> bool:
    return isinstance(name, str) and _SAFE_NAME.fullmatch(name) is not None


def _quote(name: str) -> str:
    """A checked table or column name, wrapped in double quotes for SQL. Called only with names
    that came out of the data dictionary; the check here is a second lock on the same door."""
    if not _is_safe_name(name):
        raise ToolError("that table or column name cannot be used")
    return '"' + name + '"'


def _dtype_from_declared(declared: str) -> str:
    """SQLite's declared column type -> one of our dtypes, for tables with no _columns entry."""
    declared = (declared or "").upper()
    if "INT" in declared:
        return "integer"
    if "REAL" in declared or "FLOA" in declared or "DOUB" in declared or "NUM" in declared:
        return "number"
    return "text"


def _data_dictionary(conn: sqlite3.Connection, count_rows: bool = False) -> dict[str, dict]:
    """The tables and columns the model is allowed to name, read from the database itself.

    {table: {"kind": str, "rows": int | None, "columns": {name: {"dtype": str, "role": str | None}}}}

    Tables: the four standard tables, plus every table listed in _tables. Internal tables
    (names starting with "_" or "sqlite_") can never appear, because a name must pass
    _is_safe_name and must really exist. Columns come from SQLite's own table info; their
    dtype and role come from _columns when that table has them."""
    existing = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    tables: dict[str, dict] = {}
    for name in CANONICAL_TABLES:
        if name in existing:
            tables[name] = {"kind": CANONICAL_KINDS[name], "rows": None, "columns": {}}
    if "_tables" in existing:
        listed = conn.execute("SELECT table_name, row_count, kind FROM _tables "
                              "ORDER BY row_count DESC, table_name").fetchall()
        for r in listed:
            name = r["table_name"]
            if name in tables or name not in existing:
                continue
            if not _is_safe_name(name) or name.startswith("sqlite_"):
                continue
            tables[name] = {"kind": _clean_text(r["kind"] or "other", 20), "rows": r["row_count"], "columns": {}}

    for name, info in tables.items():
        for col in conn.execute(f"PRAGMA table_info({_quote(name)})").fetchall():
            if not _is_safe_name(col["name"]):
                continue                                 # a name we would not put in SQL is not offered
            dtype = "date" if (name, col["name"]) in CANONICAL_DATE_COLUMNS else _dtype_from_declared(col["type"])
            info["columns"][col["name"]] = {"dtype": dtype, "role": None}
        if count_rows and info["rows"] is None:
            info["rows"] = conn.execute(f"SELECT COUNT(*) AS n FROM {_quote(name)}").fetchone()["n"]

    if "_columns" in existing:
        for r in conn.execute("SELECT table_name, column_name, dtype, role FROM _columns").fetchall():
            column = tables.get(r["table_name"], {"columns": {}})["columns"].get(r["column_name"])
            if column is None:
                continue
            if r["dtype"] in DTYPES:
                column["dtype"] = r["dtype"]
            if r["role"] in ROLES:
                column["role"] = r["role"]
    return tables


def _legacy_capabilities(conn: sqlite3.Connection) -> dict:
    """For a database with no _capabilities table (the built-in store): work out the same
    yes/no answers from what the four tables hold. Only describe_data uses this."""
    def has(sql: str) -> bool:
        try:
            return conn.execute(sql).fetchone() is not None
        except sqlite3.Error:
            return False

    sales = has("SELECT 1 FROM sales LIMIT 1")
    priced = sales and has("SELECT 1 FROM sales WHERE unit_price_at_sale IS NOT NULL LIMIT 1")
    cost = has("SELECT 1 FROM products WHERE unit_cost IS NOT NULL LIMIT 1")
    stock = has("SELECT 1 FROM inventory LIMIT 1")
    lead = has("SELECT 1 FROM products p JOIN suppliers s ON s.supplier_id = p.supplier_id "
               "WHERE s.lead_time_days IS NOT NULL LIMIT 1")
    available = {
        "sales_analysis": sales,
        "revenue": priced,
        "margin": priced and cost,
        "inventory_status": sales and stock,
        "supplier_terms": lead,
        "restock_planning": sales and stock and cost,
        "discount_evaluation": priced and cost,
    }
    return {name: {"available": bool(on), "reason": "" if on else CAPABILITY_MESSAGES[name]}
            for name, on in available.items()}


def describe_data(db_path=None) -> dict:
    """What is loaded: tables, columns (name, dtype, role), row counts, the date range of the
    sales, and which analyses the data supports. At most DESCRIBE_TABLES_MAX tables and
    DESCRIBE_COLUMNS_MAX columns per table; the result says how many were left out."""
    with _database(db_path) as conn:
        dictionary = _data_dictionary(conn, count_rows=True)
        caps = _load_capabilities(conn)
        if caps is None:
            caps = _legacy_capabilities(conn)
        assumptions = _load_assumptions(conn)
        first = last = None
        if "sales" in dictionary:
            row = conn.execute("SELECT MIN(sale_date) AS first, MAX(sale_date) AS last FROM sales").fetchone()
            first = None if row["first"] is None else str(row["first"])[:10]
            last = None if row["last"] is None else str(row["last"])[:10]

    tables, columns_left = [], DESCRIBE_COLUMNS_TOTAL
    names = list(dictionary)
    for name in names[:DESCRIBE_TABLES_MAX]:
        info = dictionary[name]
        entry = {"name": name, "kind": info["kind"], "rows": info["rows"]}
        if info["rows"]:                                 # an empty table's columns are of no use
            take = min(DESCRIBE_COLUMNS_MAX, columns_left)
            shown = list(info["columns"].items())[:take]
            columns_left -= len(shown)
            entry["columns"] = []
            for column_name, column in shown:
                described = {"name": column_name, "dtype": column["dtype"]}
                if column["role"] and column["role"] != column_name:
                    described["role"] = column["role"]
                entry["columns"].append(described)
            if len(info["columns"]) > len(shown):
                entry["columns_not_shown"] = len(info["columns"]) - len(shown)
        tables.append(entry)

    capabilities = {}
    for name, cap in caps.items():
        if name not in CAPABILITY_MESSAGES:
            continue                                     # only the capabilities this code knows
        capabilities[name] = {"available": True} if cap["available"] else {
            "available": False, "reason": cap["reason"] or CAPABILITY_MESSAGES[name]}
    return {
        "tables": tables,
        "tables_not_shown": max(0, len(names) - DESCRIBE_TABLES_MAX),
        "date_range": {"first": first, "last": last},
        "capabilities": capabilities,
        "assumptions": assumptions,
        "notes": ["sales, products, inventory and suppliers are the standard tables the other tools use; "
                  "tables starting with t_ are the uploaded sheets after cleaning",
                  "use these exact table and column names in query_data"],
    }


# --------------------------------------------------------------------------- #
# tool 7: safe aggregate query
# --------------------------------------------------------------------------- #
# The model picks from these fixed lists. The SQL text on the right is written here, never by the model.
AGGREGATES = {
    "sum": "SUM({col})",
    "avg": "AVG({col})",
    "min": "MIN({col})",
    "max": "MAX({col})",
    "count": "COUNT({col})",
    "count_distinct": "COUNT(DISTINCT {col})",
}
AGGREGATE_ALIASES = {"average": "avg", "mean": "avg", "total": "sum"}
FILTER_OPS = {
    "=": "{col} = ?",
    "!=": "{col} != ?",
    ">": "{col} > ?",
    ">=": "{col} >= ?",
    "<": "{col} < ?",
    "<=": "{col} <= ?",
    "between": "{col} BETWEEN ? AND ?",
    "in": "{col} IN ({marks})",
    "contains": "instr(lower({col}), lower(?)) > 0",
}
FILTER_OP_ALIASES = {"==": "=", "<>": "!="}
DATE_BUCKETS = {
    "day": "substr({col}, 1, 10)",
    "week": "date(substr({col}, 1, 10), 'weekday 0', '-6 days')",     # the Monday that starts the week
    "month": "substr({col}, 1, 7)",
    "year": "substr({col}, 1, 4)",
}
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")
FILTER_TEXT_MAX = 200      # longest text value accepted in a filter
FILTER_IN_MAX = 50         # most values in one "in" filter


def _as_list(name: str, value: Any, lo: int, hi: int) -> list:
    """Read an argument that should be a list. A single item is accepted as a list of one."""
    if value is None:
        value = []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list):
        raise ToolError(f"{name} must be a list")
    if not lo <= len(value) <= hi:
        raise ToolError(f"{name} must have between {lo} and {hi} entries, got {len(value)}")
    return value


def _check_keys(name: str, entry: Any, allowed: tuple) -> dict:
    if not isinstance(entry, dict):
        raise ToolError(f"each entry of {name} must be an object with the keys {list(allowed)}")
    extra = [str(k)[:30] for k in entry if k not in allowed]
    if extra:
        raise ToolError(f"{name} entries may only have the keys {list(allowed)}; remove {extra}")
    return entry


def _check_column(name: Any, table: str, columns: dict) -> str:
    """A column name must be exactly one of the table's columns."""
    if isinstance(name, str) and name.strip().lower() in columns:
        return name.strip().lower()
    shown = ", ".join(list(columns)[:DESCRIBE_COLUMNS_MAX])
    raise ToolError(f"table {table!r} has no column {_clean_text(name, 40)!r}. Its columns are: {shown}")


def _check_value(value: Any, column: str, dtype: str) -> Any:
    """One filter value: a plain number, true/false or short text. It is only ever passed to
    SQLite as a bound parameter, so whatever it contains is compared as data, never run."""
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolError(f"the filter value for {column} must be an ordinary number")
        return value
    if not isinstance(value, str):
        raise ToolError(f"the filter value for {column} must be a number, true/false or text")
    if len(value) > FILTER_TEXT_MAX:
        raise ToolError(f"the filter value for {column} is too long (at most {FILTER_TEXT_MAX} characters)")
    if dtype == "date":
        if not _ISO_DATE.fullmatch(value.strip()):
            raise ToolError(f"{column} is a date; give the filter value as yyyy-mm-dd, e.g. '2026-01-31'")
        return value.strip()
    if dtype in ("number", "integer"):                   # a number that arrived as text, e.g. "100"
        try:
            number = float(value)
        except ValueError:
            raise ToolError(f"{column} holds numbers; the filter value must be a number, got {_clean_text(value, 40)!r}")
        if not math.isfinite(number):
            raise ToolError(f"the filter value for {column} must be an ordinary number")
        return int(number) if number == int(number) else number
    return value


def _value_list(value: Any) -> Any:
    """'between' and 'in' need a list. A model sometimes sends the list as JSON text; read that too."""
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _build_filters(filters: Any, table: str, columns: dict) -> tuple[list[str], list]:
    """filters -> (SQL conditions with ? marks, the values to bind to those marks)."""
    conditions, params = [], []
    for entry in _as_list("filters", filters, 0, 6):
        entry = _check_keys("filters", entry, ("column", "op", "value"))
        column = _check_column(entry.get("column"), table, columns)
        dtype = columns[column]["dtype"]
        op = str(entry.get("op", "=")).strip().lower()
        op = FILTER_OP_ALIASES.get(op, op)
        if op not in FILTER_OPS:
            raise ToolError(f"filter op must be one of {list(FILTER_OPS)}, got {_clean_text(entry.get('op'), 30)!r}")
        value = entry.get("value")
        if op in ("between", "in"):
            value = _value_list(value)
        if op == "between":
            if not isinstance(value, list) or len(value) != 2:
                raise ToolError(f"a 'between' filter on {column} needs a list of two values: [low, high]")
            values = [_check_value(v, column, dtype) for v in value]
        elif op == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= FILTER_IN_MAX:
                raise ToolError(f"an 'in' filter on {column} needs a list of 1 to {FILTER_IN_MAX} values")
            values = [_check_value(v, column, dtype) for v in value]
        elif op == "contains":
            if not isinstance(value, str) or not value or len(value) > FILTER_TEXT_MAX:
                raise ToolError(f"a 'contains' filter on {column} needs a short piece of text")
            values = [value]
        else:
            values = [_check_value(value, column, dtype)]
        marks = ", ".join("?" * len(values))
        conditions.append(FILTER_OPS[op].format(col=_quote(column), marks=marks))
        params += values
    return conditions, params


def _build_metrics(metrics: Any, table: str, columns: dict) -> tuple[list[str], list[str], dict[str, str]]:
    """metrics -> (SQL expressions, output names, {source column: output name})."""
    expressions, names, by_column = [], [], {}
    for entry in _as_list("metrics", metrics, 1, 5):
        entry = _check_keys("metrics", entry, ("column", "agg", "times"))
        agg = str(entry.get("agg", "")).strip().lower()
        agg = AGGREGATE_ALIASES.get(agg, agg)
        if agg not in AGGREGATES:
            raise ToolError(f"agg must be one of {list(AGGREGATES)}, got {_clean_text(entry.get('agg'), 30)!r}")
        if entry.get("column") == "*":
            if agg != "count":
                raise ToolError("column '*' can only be used with agg 'count' (count the rows)")
            expressions.append("COUNT(*)")
            name = "count_rows"
        elif entry.get("times") not in (None, ""):
            # two number columns multiplied row by row, then aggregated: money = quantity x unit price.
            # Both names are checked against the data dictionary like any other column.
            column = _check_column(entry.get("column"), table, columns)
            other = _check_column(entry.get("times"), table, columns)
            if agg not in ("sum", "avg", "min", "max"):
                raise ToolError("'times' can only be used with sum, avg, min or max")
            for part in (column, other):
                if columns[part]["dtype"] not in NUMERIC_DTYPES:
                    raise ToolError(f"{part} is {columns[part]['dtype']}, not a number, so it cannot be multiplied")
            expressions.append(AGGREGATES[agg].format(col=f"({_quote(column)} * {_quote(other)})"))
            name = f"{agg}_{column}_times_{other}"
        else:
            column = _check_column(entry.get("column"), table, columns)
            if agg in ("sum", "avg") and columns[column]["dtype"] not in NUMERIC_DTYPES:
                raise ToolError(f"{column} is {columns[column]['dtype']}, not a number, so it cannot be used "
                                f"with {agg}; use count, count_distinct, min or max")
            expressions.append(AGGREGATES[agg].format(col=_quote(column)))
            name = f"{agg}_{column}"
            by_column[column] = name if column not in by_column else ""     # "" = more than one metric uses it
        if name in names:
            raise ToolError(f"the metric {name} is listed twice")
        names.append(name)
    return expressions, names, by_column


def _build_groups(group_by: Any, date_bucket: Any, table: str, columns: dict) -> tuple[list[str], list[str]]:
    """group_by and date_bucket -> (SQL expressions, output names)."""
    expressions, names = [], []
    for column in _as_list("group_by", group_by, 0, 3):
        column = _check_column(column, table, columns)
        if column in names:
            raise ToolError(f"{column} is listed twice in group_by")
        expressions.append(_quote(column))
        names.append(column)
    if date_bucket is not None:
        bucket = _check_keys("date_bucket", date_bucket, ("column", "by"))
        column = _check_column(bucket.get("column"), table, columns)
        by = str(bucket.get("by", "")).strip().lower()
        if by not in DATE_BUCKETS:
            raise ToolError(f"date_bucket 'by' must be one of {list(DATE_BUCKETS)}, got {_clean_text(bucket.get('by'), 30)!r}")
        if columns[column]["dtype"] != "date":
            raise ToolError(f"{column} is {columns[column]['dtype']}, not a date, so it cannot be used in date_bucket")
        expressions.append(DATE_BUCKETS[by].format(col=_quote(column)))
        names.append(f"{column}_{by}")
    return expressions, names


def _result_cell(value: Any, notes: set) -> Any:
    """One value of a result row, made small and safe to show."""
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 2) if abs(value) >= 1 else round(value, 4)
    if isinstance(value, bytes):
        return "[binary data]"
    text = _clean_text(value, QUERY_TEXT_MAX)
    if len(str(value)) > QUERY_TEXT_MAX:
        notes.add(f"text values are cut to {QUERY_TEXT_MAX} characters")
    return text


def query_data(table: str, metrics: list, group_by: list | None = None, filters: list | None = None,
               order_by: str | None = None, descending: bool = True, limit: int = 20,
               date_bucket: dict | None = None, db_path=None) -> dict:
    """Totals, averages and counts over ONE table, optionally grouped and filtered.

    Safe by construction:
      * `table` and every column must match the data dictionary exactly (_data_dictionary);
      * aggregates, comparison operators and date buckets are looked up in fixed tables above;
      * every filter value and the row limit are bound parameters (the ? marks);
      * the connection is read-only and the query is stopped after QUERY_SECONDS_MAX seconds.
    """
    notes: set[str] = set()
    limit = _check_top_n("limit", limit, QUERY_LIMIT_MAX)       # a larger limit is cut to the maximum
    if not isinstance(descending, bool):
        raise ToolError("descending must be true or false")

    with _database(db_path) as conn:
        dictionary = _data_dictionary(conn)
        if not isinstance(table, str) or table.strip().lower() not in dictionary:
            raise ToolError(f"there is no table {_clean_text(table, 40)!r}. The tables are: {', '.join(dictionary)}. "
                            f"Call describe_data to see their columns.")
        table = table.strip().lower()
        columns = dictionary[table]["columns"]

        group_sql, group_names = _build_groups(group_by, date_bucket, table, columns)
        metric_sql, metric_names, by_column = _build_metrics(metrics, table, columns)
        conditions, params = _build_filters(filters, table, columns)
        names = group_names + metric_names

        # ORDER BY and GROUP BY use column positions (1, 2, ...), so no name is needed there at all
        if order_by is None:                             # default: the time bucket, else the first metric
            order_name = group_names[-1] if date_bucket is not None else metric_names[0]
        elif isinstance(order_by, str) and order_by.strip().lower() in names:
            order_name = order_by.strip().lower()
        elif isinstance(order_by, str) and by_column.get(order_by.strip().lower()):
            order_name = by_column[order_by.strip().lower()]          # "quantity" means "sum_quantity"
        else:
            raise ToolError(f"order_by must be one of {names}, got {_clean_text(order_by, 40)!r}")
        order_position = names.index(order_name) + 1
        group_positions = [str(n) for n in range(1, len(group_names) + 1)]

        # NOT INDEXED: read the table straight through. With an index SQLite may fetch rows one by
        # one in index order, which measured 3x slower for a group-by over a million rows.
        sql = "SELECT " + ", ".join(group_sql + metric_sql) + " FROM " + _quote(table) + " NOT INDEXED"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        if group_positions:
            sql += " GROUP BY " + ", ".join(group_positions)
        sql += f" ORDER BY {order_position} " + ("DESC" if descending else "ASC")
        if group_positions:
            sql += ", " + ", ".join(group_positions)     # a fixed order among equal values
        sql += " LIMIT ?"

        # stop a query that runs too long: SQLite calls this every 10,000 steps
        deadline = time.monotonic() + QUERY_SECONDS_MAX
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        try:
            fetched = conn.execute(sql, params + [limit + 1]).fetchall()   # one extra row = "there is more"
        except sqlite3.OperationalError as e:
            if "interrupt" in str(e).lower():
                raise ToolError(f"that query ran for more than {QUERY_SECONDS_MAX} seconds and was stopped. "
                                f"Add a filter (for example a date range) or use fewer group_by columns.")
            raise

    truncated = len(fetched) > limit
    if truncated:
        notes.add(f"more rows exist than the {limit} shown; add filters or change order_by to see others")
    rows = [[_result_cell(v, notes) for v in row] for row in fetched[:limit]]
    return {
        "table": table,
        "columns": names,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "notes": sorted(notes),
    }


# --------------------------------------------------------------------------- #
# tool 8: restock scenarios side by side
# --------------------------------------------------------------------------- #
COMMON_TARGET = "aggressive"       # the one target every scenario is measured against, like for like
# The figures compared across scenarios. budget_left is left out: it mirrors total_cost.
SCENARIO_METRICS = ("total_cost", "units", "products_covered", "products_fully_funded",
                    "stockout_risk_remaining", "products_short_before_second_order",
                    "min_days_of_cover_after_delivery", "avg_days_of_cover_after_delivery",
                    "unfunded_need", "unfunded_need_vs_aggressive_target")
SCENARIO_DEFINITIONS = {
    "stockout_risk_remaining": "at stockout risk now and lead time still not covered; always the same in "
                               "all three, because lead-time needs are bought first in every scenario",
    "products_short_before_second_order": "stock after this order lasts less than two lead times",
    "days_of_cover_after_delivery": "min and avg over the ordered products",
    "unfunded_need": "dollars short of THIS scenario's own target; not a like-for-like comparison",
    "unfunded_need_vs_aggressive_target": "dollars short of the aggressive target; the same yardstick "
                                          "for all three",
}
SEARCH_STEPS_MAX = 500_000         # work limit for the budget search: budgets tried x products to order
SEARCH_BUDGETS_MAX = 5_000         # and never more than this many budgets


def _order_quantities(prepared: dict) -> dict[str, int]:
    """{product_id: units ordered} for one scenario, after _spend has run on it."""
    return {c["product_id"]: int(c["ordered"]) for c in prepared["candidates"] if c["ordered"] > 0}


def _same_orders(prepared: dict[str, dict], budget: float) -> bool:
    """Do the three scenarios place exactly the same order at this budget?"""
    orders = []
    for one in prepared.values():
        _spend(one["candidates"], budget)
        orders.append(_order_quantities(one))
    return orders[0] == orders[1] == orders[2]


def _budget_where_orders_differ(prepared: dict[str, dict], budget: float) -> tuple[int | None, int]:
    """Called when the three orders are the same at `budget`. Tries every whole-dollar budget
    above it, in order, until the orders are no longer the same.

    Returns (first budget where they differ or None, last budget tried). Every budget is tried,
    none skipped, because the answer does not grow steadily: on test data the orders differed
    in a window only $59 wide, which a faster halving search missed. The price is a work limit:
    on a big store only a short range of budgets can be tried, and the result says how far."""
    products = max(1, sum(len(one["candidates"]) for one in prepared.values()))
    tries = min(SEARCH_BUDGETS_MAX, SEARCH_STEPS_MAX // products)
    start = math.floor(budget) + 1
    for candidate_budget in range(start, start + tries):
        if not _same_orders(prepared, candidate_budget):
            return candidate_budget, candidate_budget
    return None, start + tries - 1


def _scenario_summary(full: dict, stocked: list[dict], common_targets: dict[str, dict]) -> dict:
    """The headline numbers of one allocation, for the side-by-side view.
    `stocked` is every stocked product; `common_targets` is {product_id: candidate} of the
    aggressive scenario, the shared yardstick."""
    candidates = full["candidates"]
    ordered = [c for c in candidates if c["ordered"] > 0]
    quantities = _order_quantities(full)
    # still at risk: stockout-risk products whose order does not cover their lead time, plus
    # stockout-risk products that cannot be ordered at all (no unit cost)
    at_risk = len([1 for c in candidates if c["status"] == "stockout_risk" and c["ordered"] < c["urgent_qty"]])
    at_risk += len([1 for e in full["no_cost"] if e["_status"] == "stockout_risk"])
    # would run out before a second order could arrive: cover after this order < two lead times.
    # Counted over every stocked product that sells and has a lead time, ordered or not.
    short = 0
    for item in stocked:
        velocity, lead = item["avg_daily_units_30d"], item["supplier_lead_time_days"]
        if velocity and lead is not None:
            cover_after = (item["on_hand"] + quantities.get(item["product_id"], 0)) / velocity
            short += 1 if cover_after < 2 * lead else 0
    cover = [(c["on_hand"] + c["ordered"]) / c["velocity"] for c in ordered]
    # the same yardstick for every scenario: what is still missing to reach the aggressive target
    vs_common = sum(t["unit_cost"] * max(0, t["target_qty"] - quantities.get(pid, 0))
                    for pid, t in common_targets.items())
    # short lines: product and quantity. plan_restock gives the cost and cover of each line.
    lines = [{"product_id": c["product_id"], "product_name": c["product_name"], "order_qty": int(c["ordered"])}
             for c in ordered[:SCENARIO_LINES_MAX]]
    return {
        "target_days_cover_beyond_lead_time": full["target_days"],
        "total_cost": _r2(full["budget"] - full["remaining"]),
        "budget_left": _r2(full["remaining"]),
        "units": int(sum(c["ordered"] for c in ordered)),
        "products_covered": len(ordered),               # products that get an order line
        "products_fully_funded": len([1 for c in ordered if c["ordered"] >= c["target_qty"]]),
        "stockout_risk_remaining": at_risk,
        "products_short_before_second_order": short,
        "min_days_of_cover_after_delivery": round(min(cover), 1) if cover else None,
        "avg_days_of_cover_after_delivery": round(sum(cover) / len(cover), 1) if cover else None,
        "unfunded_need": _r2(sum(c["unit_cost"] * (c["target_qty"] - c["ordered"]) for c in candidates)),
        "unfunded_need_vs_aggressive_target": _r2(vs_common),
        "order_lines": lines,
        "order_lines_not_shown": len(ordered) - len(lines),
    }


def _scenario_differences(scenarios: dict) -> tuple[list[dict], list[str]]:
    """(the metrics that differ, each with the two gaps between neighbours; the metrics that do not)."""
    differences, same = [], []
    for metric in SCENARIO_METRICS:
        low, mid, high = (scenarios[name][metric] for name in ("conservative", "moderate", "aggressive"))
        if low == mid == high:
            same.append(metric)
            continue
        known = None not in (low, mid, high)             # a scenario with no order has no cover figures
        differences.append({                             # the three values themselves are under "scenarios"
            "metric": metric,
            "moderate_minus_conservative": _money_or_count(mid - low) if known else None,
            "aggressive_minus_moderate": _money_or_count(high - mid) if known else None,
        })
    return differences, same


def _order_qty_differences(prepared: dict[str, dict]) -> list[dict]:
    """Per product, the units each scenario orders, for the products where they are not all the
    same; the biggest spread first."""
    quantities: dict[str, dict] = {}
    for name, one in prepared.items():
        for pid, units in _order_quantities(one).items():
            entry = quantities.setdefault(pid, {"product_id": pid, "conservative": 0, "moderate": 0, "aggressive": 0})
            entry[name] = units
    changed = [q for q in quantities.values() if not q["conservative"] == q["moderate"] == q["aggressive"]]
    changed.sort(key=lambda q: -(max(q["conservative"], q["moderate"], q["aggressive"])
                                 - min(q["conservative"], q["moderate"], q["aggressive"])))
    return changed


def _identical_orders_note(any_order: bool, differ_from: int | None, searched_to: int) -> dict:
    """The fields added when all three scenarios place the same order: why, and from which
    budget on they stop doing so (or how far the search looked without finding one)."""
    if any_order:
        reason = ("All three scenarios place the same order with this budget. Each one first buys what "
                  "products need to last through their supplier lead time, then tops up the most urgent "
                  "products, and the budget runs out before any scenario reaches its own target.")
    else:
        reason = "No scenario can place an order with this budget: it does not cover any product's minimum order."
    if differ_from is not None:
        reason += f" The orders first differ at a budget of {differ_from} dollars."
    else:
        reason += (f" They stay the same for every whole-dollar budget up to {searched_to}; "
                   f"larger budgets were not checked.")
    return {
        "identical_orders_reason": reason,
        "orders_differ_from_budget": differ_from,
        "orders_identical_up_to_budget": searched_to if differ_from is None else differ_from - 1,
    }


def compare_scenarios(budget: float, db_path=None) -> dict:
    """plan_restock for conservative, moderate and aggressive with the same budget, side by side,
    plus the numbers that differ between them and by how much. The data is read once.

    Three things a reader must not get wrong are stated in the result itself:
      * identical_orders: with a small budget all three place the same order, because the money
        runs out on the most urgent products before any strategy reaches its target. The result
        then also gives the first whole-dollar budget at which the orders differ, when that
        budget is within the range _budget_where_orders_differ can try;
      * stockout_risk_remaining is the same in all three by construction (see SCENARIO_DEFINITIONS);
      * unfunded_need is measured against each scenario's own target; the like-for-like figure
        is unfunded_need_vs_aggressive_target."""
    budget = _check_budget(budget)
    with _database(db_path) as conn:
        caps = _load_capabilities(conn)
        _require(caps, "restock_planning")
        inputs = _restock_inputs(conn)
    prepared = {name: _candidates(inputs, name) for name in STRATEGY_TARGET_DAYS}
    first = prepared["conservative"]
    _require_lead_times(inputs, first)

    identical = _same_orders(prepared, budget)
    differ_from, searched_to = _budget_where_orders_differ(prepared, budget) if identical else (None, None)
    for one in prepared.values():                        # the search moved the orders; set them for this budget
        one["budget"] = budget
        one["remaining"] = _spend(one["candidates"], budget)
    common_targets = {c["product_id"]: c for c in prepared[COMMON_TARGET]["candidates"]}
    scenarios = {name: _scenario_summary(one, inputs["items"], common_targets) for name, one in prepared.items()}
    differences, same = _scenario_differences(scenarios)

    gaps, left_out = [], {}
    for key, entries, why in (("excluded_missing_cost", first["no_cost"], "they have no unit cost"),
                              ("excluded_missing_lead_time", first["no_lead"], "their supplier lead time is unknown")):
        few = len(entries) <= SCENARIO_LEFT_OUT_MAX
        left_out[key] = _public(entries) if few else []
        if entries:
            gaps.append(f"{len(entries)} products are left out of every scenario because {why}"
                        + ("" if few else " (too many to list here; plan_restock shows examples)"))

    result = {"budget": _r2(budget), "identical_orders": identical}
    if identical:
        any_order = scenarios["conservative"]["units"] > 0
        result.update(_identical_orders_note(any_order, differ_from, searched_to))
    result.update({
        "scenarios": scenarios,
        "differences": differences,
        "same_in_all_scenarios": same,
        "order_qty_differences": _order_qty_differences(prepared)[:SCENARIO_DIFFS_MAX],
        "stockout_risk_now": len([1 for i in inputs["items"] if i["status"] == "stockout_risk"]),
        "excluded_missing_cost": left_out["excluded_missing_cost"],
        "excluded_missing_lead_time": left_out["excluded_missing_lead_time"],
        "data_gaps": gaps,
        "definitions": SCENARIO_DEFINITIONS,
        "assumptions": ["same method and assumptions as plan_restock; only the days of cover targeted "
                        "beyond lead time differ"] + inputs["notes"],
    })
    return result


# --------------------------------------------------------------------------- #
# tool 9: this period against the one before
# --------------------------------------------------------------------------- #
def compare_periods(days: int = 30, top_n: int = 5, db_path=None) -> dict:
    """The latest `days` days against the `days` before: units, net revenue and gross profit,
    each with change and percent change, and the `top_n` products behind the change."""
    days = _check_int("days", days, 1, 365)
    top_n = _check_top_n("top_n", top_n, 20)
    with _database(db_path) as conn:
        caps = _load_capabilities(conn)
        _require(caps, "sales_analysis")
        end = _as_of(conn)
        start = end - timedelta(days=days)
        prev_start = start - timedelta(days=days)
        cur = _units_by_product(conn, start, end)
        prev = _units_by_product(conn, prev_start, start)
        first_sale = str(conn.execute("SELECT MIN(sale_date) AS d FROM sales").fetchone()["d"])[:10]
        products = conn.execute("SELECT product_id, product_name, unit_cost FROM products").fetchall()

    revenue_on, margin_on = _is_on(caps, "revenue"), _is_on(caps, "margin")
    catalog = {str(p["product_id"]): p for p in products}
    totals = {"units": [0, 0], "revenue": [0.0, 0.0], "profit": [0.0, 0.0]}     # [current, previous]
    no_cost_sold = 0
    movers = []
    for pid in sorted(set(cur) | set(prev)):
        product = catalog.get(pid)
        unit_cost = None if product is None else product["unit_cost"]
        now = _figures(cur.get(pid), unit_cost)          # (units, revenue, cogs, gross profit)
        before = _figures(prev.get(pid), unit_cost)
        for slot, figures in enumerate((now, before)):
            totals["units"][slot] += figures[0]
            totals["revenue"][slot] += figures[1] or 0.0
            totals["profit"][slot] += figures[3] or 0.0
        if unit_cost is None:
            no_cost_sold += 1
        mover = {
            "product_id": pid,
            "product_name": pid if product is None else _name(product["product_name"]),
            "units_current": now[0], "units_previous": before[0], "units_change": now[0] - before[0],
        }
        if revenue_on:
            mover["net_revenue_current"] = _r2(now[1])
            mover["net_revenue_previous"] = _r2(before[1])
            mover["net_revenue_change"] = None if now[1] is None or before[1] is None else _r2(now[1] - before[1])
        movers.append(mover)

    ranked_by = "net_revenue_change" if revenue_on else "units_change"
    movers.sort(key=lambda m: -abs(m[ranked_by] or 0))   # biggest move first, up or down
    gaps = []
    if not revenue_on:
        gaps.append(CAPABILITY_MESSAGES["revenue"])
    elif not margin_on:
        gaps.append(CAPABILITY_MESSAGES["margin"])
    elif no_cost_sold:
        gaps.append(f"gross profit leaves out {no_cost_sold} products sold in these periods that have no unit cost")
    if first_sale > (prev_start + timedelta(days=1)).isoformat():
        gaps.append(f"the sales data starts on {first_sale}, so the previous period is not fully covered; "
                    f"the changes are overstated")
    return {
        "window_days": days,
        "current_period": {"start": (start + timedelta(days=1)).isoformat(), "end": end.isoformat()},
        "previous_period": {"start": (prev_start + timedelta(days=1)).isoformat(), "end": start.isoformat()},
        "units_sold": _change(totals["units"][0], totals["units"][1]),
        "net_revenue": _change(*totals["revenue"]) if revenue_on else None,
        "gross_profit_known_cost_only": _change(*totals["profit"]) if (revenue_on and margin_on) else None,
        "ranked_by": ranked_by,
        "top_changes": movers[:top_n],
        "products_with_sales": len(movers),
        "data_gaps": gaps,
    }


# --------------------------------------------------------------------------- #
# registry: name -> (callable, OpenAI tool schema)
# --------------------------------------------------------------------------- #
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_sales_summary",
            "description": "Per-product units sold, net revenue, COGS, gross profit and margin % for the last N days, with the prior period for trend. Returns the top products plus store-wide totals. Flags products whose unit cost is missing. Example: {\"days\": 30, \"sort_by\": \"units_sold\"}",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Look-back window in days (1-365). Default 30."},
                    "top_n": {"type": "integer", "description": "How many products to show (1-50). Default 20."},
                    "sort_by": {"type": "string", "enum": list(SALES_SORT_KEYS),
                                "description": "Rank products by this, highest first. Default net_revenue."},
                    "product_ids": {"type": "array", "items": {"type": "string"},
                                    "description": "Optional: show only these products (ids or names)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inventory_status",
            "description": "On-hand units, 30-day sales velocity, days of cover and a status (stockout_risk / low / ok / overstock) per product, sorted most urgent first, plus a count of products in each status. Example: {\"status\": \"stockout_risk\"}",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(INVENTORY_STATUSES),
                               "description": "Optional: show only products with this status."},
                    "top_n": {"type": "integer", "description": "How many products to show (1-50). Default 20."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_supplier_terms",
            "description": "Unit cost, minimum order quantity (MOQ), supplier and lead time for products. Omit product_ids to get the first products in the list. Example: {\"product_ids\": [\"Cast Iron Skillet\"]}",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_ids": {"type": "array", "items": {"type": "string"},
                                    "description": "Optional list of products, each a product id or a product name, e.g. ['P002','French Press']. Never guess an id: if you only know the name, pass the name."},
                    "top_n": {"type": "integer", "description": "How many products to show when product_ids is omitted (1-50). Default 20."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_restock",
            "description": "Deterministically allocate a purchasing budget across products: proposed order quantities, line costs, total cost, remaining cash, skipped products with reasons, and all assumptions. Respects MOQ and lead times. Example: {\"budget\": 1000, \"strategy\": \"moderate\"}",
            "parameters": {
                "type": "object",
                "properties": {
                    "budget": {"type": "number", "description": "Total purchasing budget in dollars, > 0"},
                    "strategy": {"type": "string", "enum": ["conservative", "moderate", "aggressive"],
                                 "description": "How many days of cover to target beyond lead time: 21 / 35 / 56"},
                },
                "required": ["budget"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_discount",
            "description": "Unit margin at a proposed discount and the 30-day volume needed to keep gross profit unchanged. Fails clearly if the product has no unit cost. Example: {\"product_id\": \"P010\", \"discount_pct\": 30}",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "A product id (P010) or a product name (Herb Garden). Never guess an id."},
                    "discount_pct": {"type": "number", "description": "Discount percentage, 0-100 exclusive"},
                },
                "required": ["product_id", "discount_pct"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_data",
            "description": "What data is loaded: table names, column names and types, row counts, the sales date range, and which analyses the data supports (with the reason when one is not possible). Call it before query_data, or when a tool says something is missing. Example: {}",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_data",
            "description": "Totals, averages and counts from ONE table, optionally grouped and filtered. Use it for questions the other tools do not answer, such as sales by channel or by month. Table and column names must come from describe_data. You never write SQL. Example: {\"table\": \"sales\", \"metrics\": [{\"column\": \"quantity\", \"agg\": \"sum\"}], \"group_by\": [\"channel\"]}",
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "One table name from describe_data."},
                    "metrics": {
                        "type": "array",
                        "description": "1 to 5 things to compute. Use column '*' with agg 'count' to count rows. For money, multiply two columns with 'times': {\"column\": \"quantity\", \"agg\": \"sum\", \"times\": \"unit_price_at_sale\"}.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string"},
                                "agg": {"type": "string", "enum": list(AGGREGATES)},
                                "times": {"type": "string", "description": "Optional second number column to multiply by, row by row."},
                            },
                            "required": ["column", "agg"],
                        },
                    },
                    "group_by": {"type": "array", "items": {"type": "string"},
                                 "description": "0 to 3 columns to group by."},
                    "filters": {
                        "type": "array",
                        "description": "0 to 6 conditions, all must hold. 'between' takes [low, high]; 'in' takes a list. Dates are yyyy-mm-dd.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column": {"type": "string"},
                                "op": {"type": "string", "enum": list(FILTER_OPS)},
                                "value": {"type": "string",
                                          "description": "A number or text; a list for 'between' and 'in'."},
                            },
                            "required": ["column", "op", "value"],
                        },
                    },
                    "date_bucket": {
                        "type": "object",
                        "description": "Group by time: {\"column\": \"sale_date\", \"by\": \"month\"}.",
                        "properties": {
                            "column": {"type": "string"},
                            "by": {"type": "string", "enum": list(DATE_BUCKETS)},
                        },
                        "required": ["column", "by"],
                    },
                    "order_by": {"type": "string",
                                 "description": "A group_by column or a result name such as sum_quantity. Default: the first metric."},
                    "descending": {"type": "boolean", "description": "Largest first. Default true."},
                    "limit": {"type": "integer", "description": "Rows to return (1-50). Default 20."},
                },
                "required": ["table", "metrics"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_scenarios",
            "description": "Runs the restock plan three times with the same budget (conservative, moderate, aggressive) and shows them side by side: cost, units, products covered, days of cover after delivery, unfunded need, what differs, and whether all three end up placing the same order. Use it when the owner asks which strategy to choose. Example: {\"budget\": 1000}",
            "parameters": {
                "type": "object",
                "properties": {"budget": {"type": "number", "description": "Total purchasing budget in dollars, > 0"}},
                "required": ["budget"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_periods",
            "description": "The latest N days against the N days before: units, net revenue and gross profit, each with change and percent change, and the products behind the change. Example: {\"days\": 30, \"top_n\": 5}",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Length of each period in days (1-365). Default 30."},
                    "top_n": {"type": "integer", "description": "How many products to list (1-20). Default 5."},
                },
                "required": [],
            },
        },
    },
]

TOOL_FUNCTIONS: dict[str, Callable[..., dict]] = {
    "get_sales_summary": get_sales_summary,
    "get_inventory_status": get_inventory_status,
    "get_supplier_terms": get_supplier_terms,
    "plan_restock": plan_restock,
    "evaluate_discount": evaluate_discount,
    "describe_data": describe_data,
    "query_data": query_data,
    "compare_scenarios": compare_scenarios,
    "compare_periods": compare_periods,
}


def dispatch(name: str, arguments: dict | str | None, db_path=None) -> dict:
    """Run a tool by name. Errors become observations, never exceptions."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as e:
            return {"error": f"tool arguments were not valid JSON: {e}"}
    if arguments is not None and not isinstance(arguments, dict):
        return {"error": "tool arguments must be a JSON object"}
    arguments = dict(arguments or {})
    if "db_path" in arguments:                           # internal parameter; never model-controlled
        return {"error": "db_path is not a tool parameter"}
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}; available tools: {sorted(TOOL_FUNCTIONS)}"}
    try:
        return fn(**arguments, db_path=db_path)
    except ToolError as e:
        return {"error": str(e)}
    except TypeError as e:  # wrong / unexpected argument names
        return {"error": f"bad arguments for {name}: {e}"}
