import json
import sqlite3

DB = "data/premierpreneur.db"


def get_inventory_status():
    """On-hand stock, 30-day sales velocity, days of cover and a status per product."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row          # lets us use row["column"] instead of row[3]

    # one row per product: stock, supplier lead time, and units sold in the last 30 days
    rows = conn.execute("""
        SELECT p.product_id, p.product_name, i.on_hand, i.reorder_point, s.lead_time_days,
               COALESCE(SUM(sa.quantity), 0) AS units_30d
        FROM products p
        JOIN inventory i ON i.product_id = p.product_id
        JOIN suppliers s ON s.supplier_id = p.supplier_id
        LEFT JOIN sales sa ON sa.product_id = p.product_id
                          AND sa.sale_date > date('2026-10-20', '-30 days')
        GROUP BY p.product_id
    """).fetchall()
    conn.close()

    products = []
    for r in rows:
        velocity = r["units_30d"] / 30                      # the average units sold per day
        cover = None if velocity == 0 else r["on_hand"] / velocity   # days until stock runs out
        if cover is None:
            status = "no_sales_last_30d"
        elif cover < r["lead_time_days"]:
            status = "stockout_risk"       # runs out before a new order could arrive
        elif cover < r["lead_time_days"] + 7:
            status = "low"
        elif cover > 120:
            status = "overstock"
        else:
            status = "ok"

        products.append({
            "product_id": r["product_id"],
            "below_reorder_point": r["on_hand"] < r["reorder_point"],
            "product_name": r["product_name"],
            "on_hand": r["on_hand"],
            "avg_daily_units_30d": round(velocity, 2),
            "days_of_cover": None if cover is None else round(cover, 1),
            "supplier_lead_time_days": r["lead_time_days"],
            "status": status,
        })

    # most urgent first, so the model sees the problems at the top
    products.sort(key=lambda p: p["days_of_cover"] if p["days_of_cover"] is not None else 9999)
    return {"as_of": "2026-10-20", "products": products}


# What the model reads. The description decides whether it picks this tool.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_inventory_status",
            "description": "On-hand units, 30-day sales velocity, days of cover and a status "
                           "(stockout_risk / low / ok / overstock) for every product, most urgent first.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

# name -> function, so the loop can run whatever the model asks for
TOOL_FUNCTIONS = {"get_inventory_status": get_inventory_status}


if __name__ == "__main__":
    print(json.dumps(get_inventory_status(), indent=2))