"""
CSV -> SQLite data layer.

The agent never reads CSVs directly. Everything goes through SQLite so the
calculation tools can query validated, de-duplicated records.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR.parent / "data"
DB_PATH = DATA_DIR / "premierpreneur.db"

SCHEMA = """
DROP TABLE IF EXISTS load_log;
DROP TABLE IF EXISTS inventory;
DROP TABLE IF EXISTS sales;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS suppliers;

CREATE TABLE suppliers (
    supplier_id     TEXT PRIMARY KEY,
    supplier_name   TEXT NOT NULL,
    lead_time_days  INTEGER NOT NULL CHECK (lead_time_days >= 0),
    payment_terms   TEXT
);

CREATE TABLE products (
    product_id    TEXT PRIMARY KEY,
    product_name  TEXT NOT NULL,
    category      TEXT,
    unit_price    REAL NOT NULL CHECK (unit_price >= 0),
    unit_cost     REAL CHECK (unit_cost IS NULL OR unit_cost >= 0),  -- NULL = missing cost
    supplier_id   TEXT REFERENCES suppliers(supplier_id),
    moq           INTEGER NOT NULL DEFAULT 1 CHECK (moq >= 1)
);

CREATE TABLE sales (
    sale_id             TEXT PRIMARY KEY,
    sale_date           TEXT NOT NULL,       -- ISO yyyy-mm-dd
    product_id          TEXT NOT NULL REFERENCES products(product_id),
    quantity            INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_at_sale  REAL NOT NULL CHECK (unit_price_at_sale >= 0),
    discount_pct        REAL NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
    channel             TEXT
);
CREATE INDEX idx_sales_date ON sales(sale_date);
CREATE INDEX idx_sales_product ON sales(product_id);

CREATE TABLE inventory (
    product_id     TEXT PRIMARY KEY REFERENCES products(product_id),
    on_hand        INTEGER NOT NULL CHECK (on_hand >= 0),
    reorder_point  INTEGER NOT NULL DEFAULT 0,
    last_counted   TEXT
);

CREATE TABLE load_log (
    table_name       TEXT,
    rows_read        INTEGER,
    rows_loaded      INTEGER,
    rows_rejected    INTEGER,
    notes            TEXT
);
"""


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def _num(value: str | None, cast=float):
    """'' -> None, otherwise cast."""
    if value is None or str(value).strip() == "":
        return None
    return cast(value)


def build_database(data_dir: Path = DATA_DIR, db_path: Path = DB_PATH) -> dict:
    """Load the four CSVs into a fresh SQLite file. Returns a load report."""
    required = ["suppliers.csv", "products.csv", "sales.csv", "inventory.csv"]
    missing = [name for name in required if not (data_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing CSV files in {data_dir}: {missing}. Run `python data/generate_data.py` first."
        )

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    report: dict[str, dict] = {}

    # suppliers
    rows = _read_csv(data_dir / "suppliers.csv")
    conn.executemany(
        "INSERT INTO suppliers VALUES (?,?,?,?)",
        [(r["supplier_id"], r["supplier_name"], int(r["lead_time_days"]), r.get("payment_terms")) for r in rows],
    )
    report["suppliers"] = {"read": len(rows), "loaded": len(rows), "rejected": 0, "notes": ""}

    # products (unit_cost may be blank -> NULL)
    rows = _read_csv(data_dir / "products.csv")
    missing_cost = [r["product_id"] for r in rows if _num(r.get("unit_cost")) is None]
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?,?,?,?)",
        [
            (
                r["product_id"], r["product_name"], r.get("category"), float(r["unit_price"]),
                _num(r.get("unit_cost")), r.get("supplier_id"), int(_num(r.get("moq"), int) or 1),
            )
            for r in rows
        ],
    )
    report["products"] = {
        "read": len(rows), "loaded": len(rows), "rejected": 0,
        "notes": f"missing unit_cost: {missing_cost}" if missing_cost else "",
    }

    # sales: de-duplicate on sale_id, reject bad rows instead of crashing
    rows = _read_csv(data_dir / "sales.csv")
    seen: set[str] = set()
    loaded, dupes, rejected = 0, 0, 0
    for r in rows:
        sid = r["sale_id"]
        if sid in seen:
            dupes += 1
            continue
        try:
            conn.execute(
                "INSERT INTO sales VALUES (?,?,?,?,?,?,?)",
                (
                    sid, r["sale_date"], r["product_id"], int(r["quantity"]),
                    float(r["unit_price_at_sale"]), float(_num(r.get("discount_pct")) or 0.0), r.get("channel"),
                ),
            )
            seen.add(sid)
            loaded += 1
        except (sqlite3.IntegrityError, ValueError):
            rejected += 1
    report["sales"] = {
        "read": len(rows), "loaded": loaded, "rejected": rejected,
        "notes": f"duplicate sale_id rows dropped: {dupes}",
    }

    # inventory
    rows = _read_csv(data_dir / "inventory.csv")
    conn.executemany(
        "INSERT INTO inventory VALUES (?,?,?,?)",
        [(r["product_id"], int(r["on_hand"]), int(_num(r.get("reorder_point"), int) or 0), r.get("last_counted")) for r in rows],
    )
    report["inventory"] = {"read": len(rows), "loaded": len(rows), "rejected": 0, "notes": ""}

    conn.executemany(
        "INSERT INTO load_log VALUES (?,?,?,?,?)",
        [(t, v["read"], v["loaded"], v["rejected"], v["notes"]) for t, v in report.items()],
    )
    conn.commit()
    conn.close()
    return report


def ensure_database(db_path: Path = DB_PATH) -> None:
    """Build the database if it does not exist yet (generating CSVs if needed)."""
    if db_path.exists():
        return
    if not (DATA_DIR / "sales.csv").exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("generate_data", DATA_DIR / "generate_data.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        module.generate()
    build_database(DATA_DIR, db_path)


if __name__ == "__main__":
    rep = build_database()
    for table, info in rep.items():
        print(f"{table:10s} read={info['read']:5d} loaded={info['loaded']:5d} rejected={info['rejected']:3d} {info['notes']}")
    print(f"database: {DB_PATH}")
