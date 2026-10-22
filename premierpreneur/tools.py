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
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta
from typing import Any, Callable

from .db import DB_PATH

STRATEGY_TARGET_DAYS = {"conservative": 21, "moderate": 35, "aggressive": 56}
OVERSTOCK_DAYS = 120


class ToolError(ValueError):
    """Raised for bad arguments or impossible requests. Message is model-facing."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _conn(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _as_of(conn: sqlite3.Connection) -> date:
    row = conn.execute("SELECT MAX(sale_date) AS d FROM sales").fetchone()
    if row is None or row["d"] is None:
        raise ToolError("sales table is empty; upload sales data before analysing.")
    return date.fromisoformat(row["d"])


def _r2(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def _check_int(name: str, value: Any, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer between {lo} and {hi}, got {value!r}")
    if not lo <= v <= hi:
        raise ToolError(f"{name} must be between {lo} and {hi}, got {v}")
    return v


def _units_by_product(conn: sqlite3.Connection, start: date, end: date) -> dict[str, dict]:
    """units, revenue (net of discount), cogs per product for start < sale_date <= end."""
    rows = conn.execute(
        """
        SELECT s.product_id,
               SUM(s.quantity)                                                   AS units,
               SUM(s.quantity * s.unit_price_at_sale * (1 - s.discount_pct/100.0)) AS revenue,
               CASE WHEN p.unit_cost IS NULL THEN NULL
                    ELSE SUM(s.quantity * p.unit_cost) END                        AS cogs
        FROM sales s JOIN products p USING (product_id)
        WHERE s.sale_date > ? AND s.sale_date <= ?
        GROUP BY s.product_id
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return {r["product_id"]: dict(r) for r in rows}


# --------------------------------------------------------------------------- #
# tool 1: sales summary
# --------------------------------------------------------------------------- #
def get_sales_summary(days: int = 30, db_path=None) -> dict:
    """Per-product units, net revenue, COGS, gross profit and margin for the
    last `days` days, plus the same window immediately before it for trend."""
    days = _check_int("days", days, 1, 365)
    conn = _conn(db_path)
    try:
        end = _as_of(conn)
        start = end - timedelta(days=days)
        prev_start = start - timedelta(days=days)
        cur = _units_by_product(conn, start, end)
        prev = _units_by_product(conn, prev_start, start)
        products = conn.execute("SELECT * FROM products ORDER BY product_id").fetchall()
    finally:
        conn.close()

    items, missing_cost = [], []
    tot_units = tot_rev = tot_gp = 0.0
    for p in products:
        pid = p["product_id"]
        c = cur.get(pid, {"units": 0, "revenue": 0.0, "cogs": None})
        units = int(c["units"] or 0)
        revenue = float(c["revenue"] or 0.0)
        cogs = None if p["unit_cost"] is None else float(c["cogs"] or 0.0)
        gp = None if cogs is None else revenue - cogs
        margin_pct = None if gp is None or revenue == 0 else gp / revenue * 100
        prev_units = int(prev.get(pid, {}).get("units", 0) or 0)
        trend = None if prev_units == 0 else (units - prev_units) / prev_units * 100
        if p["unit_cost"] is None:
            missing_cost.append(pid)
        items.append({
            "product_id": pid,
            "product_name": p["product_name"],
            "units_sold": units,
            "avg_daily_units": _r2(units / days),
            "net_revenue": _r2(revenue),
            "cogs": _r2(cogs),
            "gross_profit": _r2(gp),
            "gross_margin_pct": _r2(margin_pct),
            "units_prev_period": prev_units,
            "units_trend_pct": _r2(trend),
        })
        tot_units += units
        tot_rev += revenue
        tot_gp += gp or 0.0

    return {
        "window_days": days,
        "period": {"start": (start + timedelta(days=1)).isoformat(), "end": end.isoformat()},
        "totals": {
            "units_sold": int(tot_units),
            "net_revenue": _r2(tot_rev),
            "gross_profit_known_cost_only": _r2(tot_gp),
        },
        "products": items,
        "data_gaps": (
            [f"{pid}: unit_cost missing - COGS, gross profit and margin cannot be computed" for pid in missing_cost]
        ),
    }


# --------------------------------------------------------------------------- #
# tool 2: inventory status
# --------------------------------------------------------------------------- #
def get_inventory_status(db_path=None) -> dict:
    """On-hand, velocity, days of cover and a stock status per product.
    Velocity is average daily units over the last 30 days of sales."""
    conn = _conn(db_path)
    try:
        end = _as_of(conn)
        cur = _units_by_product(conn, end - timedelta(days=30), end)
        rows = conn.execute(
            """
            SELECT p.product_id, p.product_name, i.on_hand, i.reorder_point,
                   s.supplier_id, s.lead_time_days
            FROM products p
            JOIN inventory i USING (product_id)
            LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
            ORDER BY p.product_id
            """
        ).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        units30 = int(cur.get(r["product_id"], {}).get("units", 0) or 0)
        velocity = units30 / 30.0
        lead = int(r["lead_time_days"] or 0)
        if velocity == 0:
            cover, status = None, "no_sales_last_30d"
        else:
            cover = r["on_hand"] / velocity
            if cover < lead:
                status = "stockout_risk"       # will run out before a new order could arrive
            elif cover < lead + 7:
                status = "low"
            elif cover > OVERSTOCK_DAYS:
                status = "overstock"
            else:
                status = "ok"
        items.append({
            "product_id": r["product_id"],
            "product_name": r["product_name"],
            "on_hand": int(r["on_hand"]),
            "reorder_point": int(r["reorder_point"]),
            "below_reorder_point": bool(r["on_hand"] < r["reorder_point"]),
            "avg_daily_units_30d": _r2(velocity),
            "days_of_cover": None if cover is None else round(cover, 1),
            "supplier_lead_time_days": lead,
            "status": status,
        })
    order = {"stockout_risk": 0, "low": 1, "ok": 2, "overstock": 3, "no_sales_last_30d": 4}
    items.sort(key=lambda x: (order[x["status"]], x["days_of_cover"] if x["days_of_cover"] is not None else 1e9))
    return {
        "as_of": end.isoformat(),
        "status_definitions": {
            "stockout_risk": "days_of_cover < supplier lead time",
            "low": "days_of_cover < lead time + 7",
            "overstock": f"days_of_cover > {OVERSTOCK_DAYS}",
        },
        "products": items,
    }


# --------------------------------------------------------------------------- #
# tool 3: supplier terms
# --------------------------------------------------------------------------- #
def get_supplier_terms(product_ids: list[str] | None = None, db_path=None) -> dict:
    """Unit cost, MOQ, supplier and lead time per product."""
    if product_ids is not None:
        if not isinstance(product_ids, list) or not all(isinstance(x, str) for x in product_ids):
            raise ToolError("product_ids must be a list of product id strings, e.g. ['P001','P002']")
    conn = _conn(db_path)
    try:
        sql = """
            SELECT p.product_id, p.product_name, p.unit_cost, p.moq,
                   s.supplier_id, s.supplier_name, s.lead_time_days, s.payment_terms
            FROM products p LEFT JOIN suppliers s ON s.supplier_id = p.supplier_id
        """
        params: tuple = ()
        if product_ids:
            sql += " WHERE p.product_id IN (%s)" % ",".join("?" * len(product_ids))
            params = tuple(product_ids)
        rows = conn.execute(sql + " ORDER BY p.product_id", params).fetchall()
    finally:
        conn.close()

    found = {r["product_id"] for r in rows}
    unknown = [pid for pid in (product_ids or []) if pid not in found]
    if product_ids and not rows:
        raise ToolError(f"none of the requested product_ids exist: {product_ids}")
    items = [{
        "product_id": r["product_id"],
        "product_name": r["product_name"],
        "unit_cost": _r2(r["unit_cost"]),
        "moq": int(r["moq"]),
        "supplier_id": r["supplier_id"],
        "supplier_name": r["supplier_name"],
        "lead_time_days": r["lead_time_days"],
        "payment_terms": r["payment_terms"],
    } for r in rows]
    return {
        "products": items,
        "data_gaps": [f"{i['product_id']}: unit_cost missing" for i in items if i["unit_cost"] is None]
                     + [f"{pid}: unknown product_id" for pid in unknown],
    }


# --------------------------------------------------------------------------- #
# tool 4: restock plan
# --------------------------------------------------------------------------- #
def plan_restock(budget: float, strategy: str = "moderate", db_path=None) -> dict:
    """Allocate a fixed purchasing budget across products.

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
    Products with no unit cost are excluded and reported, never guessed.
    """
    try:
        budget = float(budget)
    except (TypeError, ValueError):
        raise ToolError(f"budget must be a number, got {budget!r}")
    if budget <= 0:
        raise ToolError(f"budget must be greater than 0, got {budget}")
    strategy = str(strategy).lower().strip()
    if strategy not in STRATEGY_TARGET_DAYS:
        raise ToolError(f"strategy must be one of {sorted(STRATEGY_TARGET_DAYS)}, got {strategy!r}")
    target_days = STRATEGY_TARGET_DAYS[strategy]

    inv = get_inventory_status(db_path)["products"]
    terms = {t["product_id"]: t for t in get_supplier_terms(None, db_path)["products"]}
    sales = {s["product_id"]: s for s in get_sales_summary(30, db_path)["products"]}

    candidates, excluded, skipped = [], [], []
    for item in inv:
        pid = item["product_id"]
        t = terms[pid]
        if t["unit_cost"] is None:
            excluded.append({"product_id": pid, "product_name": item["product_name"],
                             "reason": "unit_cost missing - cannot price an order"})
            continue
        velocity = item["avg_daily_units_30d"] or 0.0
        if velocity == 0:
            skipped.append({"product_id": pid, "product_name": item["product_name"],
                            "reason": "no sales in last 30 days"})
            continue
        horizon = t["lead_time_days"] + target_days
        target_qty = math.ceil(velocity * horizon - item["on_hand"])
        if target_qty <= 0:
            skipped.append({"product_id": pid, "product_name": item["product_name"],
                            "reason": f"already covered: {item['days_of_cover']}d on hand >= {horizon}d target"})
            continue
        urgent_qty = max(0, math.ceil(velocity * t["lead_time_days"] - item["on_hand"]))
        if 0 < urgent_qty < t["moq"]:
            urgent_qty = t["moq"]
        target_qty = max(target_qty, t["moq"])
        unit_margin = (sales[pid]["gross_profit"] or 0.0) / max(sales[pid]["units_sold"], 1)
        candidates.append({
            "product_id": pid, "product_name": item["product_name"],
            "on_hand": item["on_hand"], "velocity": velocity,
            "days_of_cover": item["days_of_cover"], "status": item["status"],
            "lead_time_days": t["lead_time_days"], "horizon_days": horizon,
            "urgent_qty": urgent_qty, "target_qty": target_qty,
            "moq": t["moq"], "unit_cost": t["unit_cost"], "unit_margin": unit_margin,
            "ordered": 0,
        })

    # urgency first (fewest days of cover), then higher unit margin
    candidates.sort(key=lambda c: (c["days_of_cover"], -c["unit_margin"]))

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

    plan, unfunded = [], []
    for c in candidates:
        if c["ordered"] > 0:
            plan.append({
                "product_id": c["product_id"],
                "product_name": c["product_name"],
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
        short = c["target_qty"] - c["ordered"]
        if short > 0:
            unfunded.append({
                "product_id": c["product_id"],
                "product_name": c["product_name"],
                "status": c["status"],
                "units_short_of_target": int(short),
                "additional_cost_to_fully_fund": _r2(short * c["unit_cost"]),
                "lead_time_covered": bool(c["ordered"] >= c["urgent_qty"]),
            })

    total_cost = budget - remaining
    full_plan_cost = sum(c["target_qty"] * c["unit_cost"] for c in candidates)
    return {
        "strategy": strategy,
        "target_days_cover_beyond_lead_time": target_days,
        "budget": _r2(budget),
        "total_cost": _r2(total_cost),
        "remaining_budget": _r2(remaining),
        "budget_needed_for_full_target_plan": _r2(full_plan_cost),
        "plan": plan,
        "unfunded": unfunded,
        "skipped": skipped,
        "excluded_missing_cost": excluded,
        "assumptions": [
            "demand = average daily units over the last 30 days, assumed constant",
            "cover target = supplier lead time + strategy target days",
            "pass 1 funds every product through its own lead time; pass 2 tops up toward target",
            "first order of a product must meet supplier MOQ; top-ups can be any size",
            "unit cost from the product catalog; shipping and duties not included",
            "ranking: fewest days of cover first, then highest unit margin",
        ],
    }


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

    conn = _conn(db_path)
    try:
        p = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
        if p is None:
            raise ToolError(f"unknown product_id {product_id!r}")
        end = _as_of(conn)
        cur = _units_by_product(conn, end - timedelta(days=30), end).get(product_id, {"units": 0, "revenue": 0.0})
        inv = conn.execute("SELECT on_hand FROM inventory WHERE product_id = ?", (product_id,)).fetchone()
    finally:
        conn.close()

    if p["unit_cost"] is None:
        raise ToolError(f"{product_id} has no unit_cost on file; margin at a discount cannot be computed")

    price, cost = float(p["unit_price"]), float(p["unit_cost"])
    units30 = int(cur["units"] or 0)
    new_price = price * (1 - discount_pct / 100)
    cur_margin, new_margin = price - cost, new_price - cost
    cur_gp = cur_margin * units30
    breakeven_units = None if new_margin <= 0 else math.ceil(cur_gp / new_margin)
    return {
        "product_id": product_id,
        "product_name": p["product_name"],
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
# registry: name -> (callable, OpenAI tool schema)
# --------------------------------------------------------------------------- #
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_sales_summary",
            "description": "Per-product units sold, net revenue, COGS, gross profit and margin % for the last N days, with the prior period for trend. Flags products whose unit cost is missing.",
            "parameters": {
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "Look-back window in days (1-365). Default 30."}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_inventory_status",
            "description": "On-hand units, 30-day sales velocity, days of cover and a status (stockout_risk / low / ok / overstock) for every product, sorted most urgent first.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_supplier_terms",
            "description": "Unit cost, minimum order quantity (MOQ), supplier and lead time for products. Omit product_ids to get all products.",
            "parameters": {
                "type": "object",
                "properties": {"product_ids": {"type": "array", "items": {"type": "string"},
                                               "description": "Optional list of product ids, e.g. ['P002','P003']"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_restock",
            "description": "Deterministically allocate a purchasing budget across products: proposed order quantities, line costs, total cost, remaining cash, skipped products with reasons, and all assumptions. Respects MOQ and lead times.",
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
            "description": "Unit margin at a proposed discount and the 30-day volume needed to keep gross profit unchanged. Fails clearly if the product has no unit cost.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "discount_pct": {"type": "number", "description": "Discount percentage, 0-100 exclusive"},
                },
                "required": ["product_id", "discount_pct"],
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
}


def dispatch(name: str, arguments: dict | str | None, db_path=None) -> dict:
    """Run a tool by name. Errors become observations, never exceptions."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as e:
            return {"error": f"tool arguments were not valid JSON: {e}"}
    arguments = dict(arguments or {})
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}; available tools: {sorted(TOOL_FUNCTIONS)}"}
    try:
        return fn(**arguments, db_path=db_path)
    except ToolError as e:
        return {"error": str(e)}
    except TypeError as e:  # wrong / unexpected argument names
        return {"error": f"bad arguments for {name}: {e}"}
