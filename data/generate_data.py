"""
Synthetic data generator for PremierPreneur.

Produces four CSV files that mimic what a small online home-goods retailer
would export from Shopify / their supplier spreadsheets:

    products.csv   - catalog with price, cost, supplier, minimum order qty
    sales.csv      - 90 days of order lines (with a few deliberate duplicates)
    inventory.csv  - current on-hand snapshot
    suppliers.csv  - lead time and payment terms

Deliberate edge cases (the proposal's evaluation plan calls for these):
    * P011 has NO unit cost        -> tools must flag missing cost, not invent one
    * P010 is a slow mover         -> overstocked, discount candidate
    * P002 is a fast mover         -> stockout risk, should be top restock priority
    * P012 demand is trending down -> prior-period comparison should show it
    * 3 duplicate sale_ids         -> loader must de-duplicate and report it

No customer data is generated. Seeded, so every run produces the same files.
"""
from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent
SEED = 4394  # CIS 4394
AS_OF = date(2026, 10, 20)  # last day of sales history
HISTORY_DAYS = 90

SUPPLIERS = [
    # supplier_id, supplier_name, lead_time_days, payment_terms
    ("S01", "Bamboo & Co. Imports", 14, "Net 30"),
    ("S02", "ThermoWorks Supply", 21, "Net 30"),
    ("S03", "HomeBasics Wholesale", 10, "Prepaid"),
    ("S04", "IronCraft Foundry", 30, "Net 45"),
]

# product_id, name, category, unit_price, unit_cost, supplier_id, moq, base_daily_demand, trend
PRODUCTS = [
    ("P001", "Bamboo Cutting Board 18in", "Prep", 24.99, 9.50, "S01", 12, 2.5, 0.0),
    ("P002", "Insulated Tumbler 20oz", "Drinkware", 19.99, 6.80, "S02", 24, 4.0, 0.0),
    ("P003", "Silicone Spatula Set (5pc)", "Utensils", 12.99, 4.10, "S03", 20, 3.0, 0.0),
    ("P004", "Cast Iron Skillet 10in", "Cookware", 34.99, 17.50, "S04", 6, 1.2, 0.0),
    ("P005", "French Press 34oz", "Drinkware", 29.99, 11.00, "S02", 12, 1.5, 0.0),
    ("P006", "Glass Food Storage Set (10pc)", "Storage", 39.99, 18.75, "S03", 8, 1.0, 0.0),
    ("P007", "Digital Kitchen Scale", "Gadgets", 15.99, 6.25, "S03", 20, 2.0, 0.0),
    ("P008", "Linen Kitchen Apron", "Textiles", 22.99, 9.90, "S01", 10, 0.8, 0.0),
    ("P009", "Spice Rack Organizer", "Storage", 27.99, 12.40, "S01", 6, 0.6, 0.0),
    ("P010", "Herb Garden Starter Kit", "Seasonal", 18.99, 7.60, "S03", 25, 0.1, 0.0),
    ("P011", "Marble Rolling Pin", "Prep", 32.99, None, "S04", 6, 0.5, 0.0),   # missing cost
    ("P012", "Stainless Measuring Cups", "Utensils", 14.99, 5.30, "S03", 20, 2.2, -0.5),  # declining
]

# on-hand chosen to produce distinct situations (days of cover is derived later)
INVENTORY = {
    "P001": (60, 30),    # ~24 days cover  -> fine for now
    "P002": (18, 60),    # ~4-5 days cover -> stockout risk, lead time 21d
    "P003": (28, 40),    # ~9 days cover   -> low
    "P004": (30, 10),    # ~25 days cover  -> borderline vs 30d lead time
    "P005": (40, 20),    # ~27 days
    "P006": (35, 12),    # ~35 days
    "P007": (22, 30),    # ~11 days -> low
    "P008": (30, 10),    # ~37 days
    "P009": (26, 8),     # ~43 days
    "P010": (48, 10),    # ~480 days -> overstock
    "P011": (14, 6),     # ~28 days but no cost on file
    "P012": (55, 25),    # ~25-35 days, demand falling
}

CHANNELS = ["web", "web", "web", "marketplace"]


def _weekday_factor(d: date) -> float:
    # small weekly seasonality: weekends a bit higher for a consumer store
    return {5: 1.25, 6: 1.15}.get(d.weekday(), 0.95)


def generate() -> dict[str, int]:
    rng = random.Random(SEED)
    counts: dict[str, int] = {}

    # suppliers.csv
    with (DATA_DIR / "suppliers.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["supplier_id", "supplier_name", "lead_time_days", "payment_terms"])
        for row in SUPPLIERS:
            w.writerow(row)
    counts["suppliers"] = len(SUPPLIERS)

    # products.csv
    with (DATA_DIR / "products.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "product_name", "category", "unit_price", "unit_cost", "supplier_id", "moq"])
        for pid, name, cat, price, cost, sup, moq, _, _ in PRODUCTS:
            w.writerow([pid, name, cat, f"{price:.2f}", "" if cost is None else f"{cost:.2f}", sup, moq])
    counts["products"] = len(PRODUCTS)

    # sales.csv - one row per order line
    rows = []
    sale_no = 10000
    start = AS_OF - timedelta(days=HISTORY_DAYS - 1)
    for day_idx in range(HISTORY_DAYS):
        d = start + timedelta(days=day_idx)
        progress = day_idx / (HISTORY_DAYS - 1)  # 0 -> 1 across the window
        for pid, name, cat, price, cost, sup, moq, base, trend in PRODUCTS:
            expected = base * _weekday_factor(d) * (1.0 + trend * progress)
            expected = max(expected, 0.0)
            # number of order lines that day ~ Poisson-ish via rounding noise
            n_orders = int(expected) + (1 if rng.random() < (expected - int(expected)) else 0)
            for _ in range(n_orders):
                sale_no += 1
                qty = 1 if rng.random() < 0.8 else 2
                disc = rng.choice([0, 0, 0, 0, 0, 10, 15])  # occasional promo
                rows.append([
                    f"SO{sale_no}", d.isoformat(), pid, qty, f"{price:.2f}", disc, rng.choice(CHANNELS),
                ])

    # deliberate duplicate rows (export glitch) - exact copies of 3 existing lines
    dupes = rng.sample(rows, 3)
    rows.extend([list(r) for r in dupes])
    rng.shuffle(rows)
    rows.sort(key=lambda r: r[1])

    with (DATA_DIR / "sales.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sale_id", "sale_date", "product_id", "quantity", "unit_price_at_sale", "discount_pct", "channel"])
        w.writerows(rows)
    counts["sales_rows"] = len(rows)
    counts["sales_duplicates_injected"] = len(dupes)

    # inventory.csv
    with (DATA_DIR / "inventory.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "on_hand", "reorder_point", "last_counted"])
        for pid, (on_hand, rop) in INVENTORY.items():
            w.writerow([pid, on_hand, rop, AS_OF.isoformat()])
    counts["inventory"] = len(INVENTORY)

    return counts


if __name__ == "__main__":
    c = generate()
    for k, v in c.items():
        print(f"{k:28s} {v}")
    print(f"written to {DATA_DIR}")
