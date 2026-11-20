"""
Building the four standard tables from whatever the upload contained.

The existing tools read four tables: products, sales, inventory, suppliers. An upload rarely
has exactly those four sheets, so this module fills them from the cleaned tables by ROLE,
not by sheet name:

  sales      from every table of kind "transactions" (a product, a date and a quantity)
  products   from every other table that has a product column, completed from the sales
             (products that only appear in sales are added; a missing list price becomes
             the latest sale price)
  inventory  from every table that has a product and an on-hand column. On a stock sheet
             the rows of one product are added up (several warehouses); on a sales sheet
             the figure on the product's most recent row is used
  suppliers  from supplier sheets, and from product or sales sheets that carry supplier
             columns (supplier, lead time, payment terms)

One rule is used wherever a product appears on several rows: the most recent row that has
a value wins. "Most recent" means the latest sale date on a sales sheet and the lowest row
on a sheet without dates.

Everything that is derived rather than read (ids made from names, prices worked out from
revenue, sale ids made up) is written to the report, so the owner can see it. Nothing is
invented: a value that is not in the file and cannot be worked out stays empty.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .roles import roles_of
from .textclean import normalise_header

PRODUCT_COLUMNS = ["product_id", "product_name", "category", "unit_price", "unit_cost", "supplier_id", "moq"]
SALES_COLUMNS = ["sale_id", "sale_date", "product_id", "quantity", "unit_price_at_sale", "discount_pct", "channel"]
INVENTORY_COLUMNS = ["product_id", "on_hand", "reorder_point", "last_counted"]
SUPPLIER_COLUMNS = ["supplier_id", "supplier_name", "lead_time_days", "payment_terms"]

COUNT_DATE_WORDS = frozenset({"counted", "count", "stocktake", "updated", "checked"})


class KeyBook:
    """Gives every name one id, for sheets that have names but no id column.

    Names are compared without regard to letter case or spacing. Ids already present in the
    file are learnt first, so "Blue Mug" in a sales sheet gets the id that the product sheet
    gives it. Names with no known id get a made-up one: ITEM-00001, ITEM-00002, ...
    """

    def __init__(self, prefix: str):
        self.prefix = prefix
        self.id_by_name = {}
        self.made_up = 0

    @staticmethod
    def _key(name: str) -> str:
        return " ".join(str(name).lower().split())

    def learn(self, ids: np.ndarray, names: np.ndarray) -> None:
        for known_id, name in zip(ids.tolist(), names.tolist()):
            if known_id is not None and name is not None:
                self.id_by_name.setdefault(self._key(name), known_id)

    def ids_for(self, names: np.ndarray) -> np.ndarray:
        result = np.empty(len(names), dtype=object)
        for row, name in enumerate(names.tolist()):
            if name is None:
                result[row] = None
                continue
            key = self._key(name)
            if key not in self.id_by_name:
                self.made_up += 1
                self.id_by_name[key] = f"{self.prefix}{self.made_up:05d}"
            result[row] = self.id_by_name[key]
        return result


# --------------------------------------------------------------------------------------
# Reading one role out of one table
# --------------------------------------------------------------------------------------
def _dtype_of(table: dict, column_name: str) -> str:
    for column in table["columns"]:
        if column["name"] == column_name:
            return column["dtype"]
    return "text"


def _header_key(table: dict, column_name: str) -> str:
    for column in table["columns"]:
        if column["name"] == column_name:
            return normalise_header(column["original_header"]) or column_name
    return column_name


def _id_text(table: dict, frame: pd.DataFrame, role: str):
    """A role's column as id text (17850 and not 17850.0), or None when the table lacks it."""
    name = roles_of(table["columns"]).get(role)
    if name is None:
        return None
    values = frame[name].to_numpy(dtype=object, na_value=None)
    if _dtype_of(table, name) == "text":
        return values
    return np.array([None if value is None else str(int(value)) for value in values.tolist()], dtype=object)


def _text(table: dict, frame: pd.DataFrame, role: str) -> pd.Series:
    """A role's column as text; all empty when the table lacks it."""
    name = roles_of(table["columns"]).get(role)
    if name is None:
        return pd.Series([None] * len(frame), dtype=object)
    values = frame[name].to_numpy(dtype=object, na_value=None)
    if _dtype_of(table, name) != "text":
        values = np.array([None if value is None else str(value) for value in values.tolist()], dtype=object)
    return pd.Series(values, dtype=object)


def _number(table: dict, frame: pd.DataFrame, role: str) -> pd.Series:
    """A role's column as numbers (NaN for empty); all NaN when the table lacks it."""
    name = roles_of(table["columns"]).get(role)
    if name is None:
        return pd.Series(np.full(len(frame), np.nan), dtype="float64")
    return pd.Series(frame[name].to_numpy(dtype="float64", na_value=np.nan), dtype="float64")


def _product_ids(table: dict, frame: pd.DataFrame, book: KeyBook) -> np.ndarray:
    ids = _id_text(table, frame, "product_id")
    if ids is not None:
        return ids
    names = _text(table, frame, "product_name").to_numpy(dtype=object, na_value=None)
    return book.ids_for(names)


def _supplier_ids(table: dict, frame: pd.DataFrame, book: KeyBook, lead_time: pd.Series) -> np.ndarray:
    """The supplier of each row: the id column, else an id for the supplier name, else (when
    the sheet has lead times but no supplier at all) one group per lead time, so that the
    lead time is not lost. The group id reads LT-14 for 14 days."""
    roles = roles_of(table["columns"])
    ids = _id_text(table, frame, "supplier_id")
    if ids is not None:
        return ids
    if "supplier_name" in roles:
        names = _text(table, frame, "supplier_name").to_numpy(dtype=object, na_value=None)
        return book.ids_for(names)
    return np.array([None if days != days else f"LT-{int(days)}" for days in lead_time.tolist()], dtype=object)


def _lead_time_days(table: dict, frame: pd.DataFrame, steps: list) -> pd.Series:
    """Lead time in whole days. A header that says weeks or months is converted."""
    days = _number(table, frame, "lead_time_days")
    name = roles_of(table["columns"]).get("lead_time_days")
    if name is None:
        return days
    tokens = set(_header_key(table, name).split("_"))
    if tokens.intersection({"week", "weeks", "wk", "wks"}):
        days = days * 7
        steps.append(("lead_time_converted", "suppliers", int(days.notna().sum()),
                      f"Column {name} is in weeks; it was multiplied by 7 to get days."))
    elif tokens.intersection({"month", "months"}):
        days = days * 30
        steps.append(("lead_time_converted", "suppliers", int(days.notna().sum()),
                      f"Column {name} is in months; it was multiplied by 30 to get days."))
    return np.floor(days.where(days >= 0) + 0.5)


# --------------------------------------------------------------------------------------
# The whole job
# --------------------------------------------------------------------------------------
def build_canonical(tables: list, frames: dict) -> tuple:
    """Build the four standard tables.

    tables: the table descriptions from prepare() (name, kind, columns with roles).
    frames: {table name: cleaned DataFrame}.
    Returns ({"products": df, "sales": df, "inventory": df, "suppliers": df}, steps) where
    steps are (step, table, count, detail) entries for the cleaning report.
    """
    steps = []
    with_product = [t for t in tables if {"product_id", "product_name"} & set(roles_of(t["columns"]))]
    sale_tables = [t for t in with_product if t["kind"] == "transactions"]
    item_tables = [t for t in with_product if t["kind"] != "transactions"]
    supplier_tables = [t for t in tables if t["kind"] == "suppliers"]

    # Learn the ids the file already has, before any id is made up from a name.
    product_book, supplier_book = KeyBook("ITEM-"), KeyBook("SUP-")
    for table in item_tables + sale_tables:
        ids = _id_text(table, frames[table["name"]], "product_id")
        if ids is not None and "product_name" in roles_of(table["columns"]):
            names = _text(table, frames[table["name"]], "product_name").to_numpy(dtype=object, na_value=None)
            product_book.learn(ids, names)
    for table in supplier_tables + item_tables + sale_tables:
        ids = _id_text(table, frames[table["name"]], "supplier_id")
        if ids is not None and "supplier_name" in roles_of(table["columns"]):
            names = _text(table, frames[table["name"]], "supplier_name").to_numpy(dtype=object, na_value=None)
            supplier_book.learn(ids, names)

    product_parts, sale_parts, supplier_parts = [], [], []
    stock_sheet_parts, stock_from_sales_parts = [], []
    for table in supplier_tables:
        supplier_parts.append(_supplier_facts(table, frames[table["name"]], supplier_book, steps)[0])
    for table in item_tables + sale_tables:
        frame = frames[table["name"]]
        is_sales = table["kind"] == "transactions"
        has_stock = "on_hand" in roles_of(table["columns"])
        product_ids = pd.Series(_product_ids(table, frame, product_book), dtype=object)
        # The day of each row on a sales sheet; a sheet without sale dates has none.
        dates = _day_only(_text(table, frame, "sale_date")) if is_sales else None
        facts, supplier_ids = _supplier_facts(table, frame, supplier_book, steps)
        if facts is not None:
            supplier_parts.append(_supplier_facts_per_product(facts, product_ids, dates, table["name"], steps))
        if is_sales:
            sale_parts.append(_sale_lines(table, frame, product_ids, dates, supplier_ids))
            if has_stock:
                stock_from_sales_parts.append(_stock_from_sales(table, frame, product_ids, dates, steps))
        else:
            product_parts.append(_product_facts(table, frame, product_ids, supplier_ids))
            if has_stock:
                stock_sheet_parts.append(_inventory_facts(table, frame, product_ids))

    if product_book.made_up:
        steps.append(("derived_product_ids", "products", product_book.made_up,
                      "There is no product id column for these products; ids ITEM-00001, ... were made from the product names."))
    if supplier_book.made_up:
        steps.append(("derived_supplier_ids", "suppliers", supplier_book.made_up,
                      "There is no supplier id column for these suppliers; ids SUP-00001, ... were made from the supplier names."))

    sales, catalog = _finish_sales(sale_parts, steps)
    products = _finish_products(product_parts, catalog, steps)
    sales = _fill_sale_prices(sales, products, sale_tables, steps)
    inventory, stock_names = _finish_inventory(stock_sheet_parts, stock_from_sales_parts, steps)
    products = _add_products_known_only_from_stock(products, inventory, stock_names, steps)
    suppliers = _finish_suppliers(supplier_parts, steps)
    return {"products": products, "sales": sales[SALES_COLUMNS], "inventory": inventory, "suppliers": suppliers}, steps


def _latest_per_product(rows: pd.DataFrame, keys=("product_id",)) -> pd.DataFrame:
    """One row per product: for every column, the value from the product's most recent row
    that has one.

    "Most recent" is by sale date when the rows carry a "sale_date" column (rows without a
    date count as oldest), otherwise the lowest row in the sheet. `keys` can name more
    columns to group by, e.g. product and supplier together.
    """
    keys = list(keys)
    rows = rows[rows[keys].notna().all(axis=1)]
    if "sale_date" in rows.columns:
        rows = rows.sort_values("sale_date", kind="stable", na_position="first")
    return rows.groupby(keys, sort=False).last()      # last() skips empty cells


# --------------------------------------------------------------------------------------
# Facts read from one table
# --------------------------------------------------------------------------------------
def _product_facts(table: dict, frame: pd.DataFrame, product_ids: pd.Series, supplier_ids) -> pd.DataFrame:
    return pd.DataFrame({
        "product_id": product_ids,
        "product_name": _text(table, frame, "product_name"),
        "category": _text(table, frame, "category"),
        "unit_price": _number(table, frame, "unit_price"),
        "unit_cost": _number(table, frame, "unit_cost"),
        "supplier_id": pd.Series(supplier_ids, dtype=object),
        "moq": _number(table, frame, "moq"),
    })


def _supplier_facts(table: dict, frame: pd.DataFrame, book: KeyBook, steps: list) -> tuple:
    """(supplier facts or None, supplier id per row) for one table."""
    roles = roles_of(table["columns"])
    lead_time = _lead_time_days(table, frame, steps)
    supplier_ids = _supplier_ids(table, frame, book, lead_time)
    if not {"supplier_name", "lead_time_days", "payment_terms"} & set(roles) and table["kind"] != "suppliers":
        return None, supplier_ids                    # only an id column: nothing to say about the supplier
    facts = pd.DataFrame({
        "supplier_id": pd.Series(supplier_ids, dtype=object),
        "supplier_name": _text(table, frame, "supplier_name"),
        "lead_time_days": lead_time,
        "payment_terms": _text(table, frame, "payment_terms"),
    })
    return facts, supplier_ids


def _supplier_facts_per_product(facts: pd.DataFrame, product_ids: pd.Series, dates, table_name: str,
                                steps: list) -> pd.DataFrame:
    """Reduce the supplier columns of a product or sales sheet to one row per product and
    supplier, keeping the most recent value of each fact.

    A sales sheet repeats the supplier and the lead time on every sale line. When the lead
    time of one product changes from row to row, the most recent one is used and the
    product is counted in the report.
    """
    rows = facts.assign(product_id=product_ids)
    if dates is not None:
        rows = rows.assign(sale_date=dates)
    pairs = ["product_id", "supplier_id"]
    usable = rows[rows[pairs].notna().all(axis=1)]
    changed = int((usable.groupby(pairs, sort=False)["lead_time_days"].nunique() > 1).sum())
    if changed:
        steps.append(("lead_time_conflicts", "suppliers", changed,
                      f"In {table_name}, products show different lead times on different rows; "
                      "the lead time on each product's most recent row was used."))
    latest = _latest_per_product(rows, keys=pairs).reset_index()
    return latest[SUPPLIER_COLUMNS]


def _inventory_facts(table: dict, frame: pd.DataFrame, product_ids: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({
        "product_id": product_ids,
        "on_hand": _number(table, frame, "on_hand"),
        "reorder_point": _number(table, frame, "reorder_point"),
        "last_counted": _count_date(table, frame),
    })


def _stock_from_sales(table: dict, frame: pd.DataFrame, product_ids: pd.Series, dates: pd.Series,
                      steps: list) -> pd.DataFrame:
    """Stock levels from a sales sheet that also has an on-hand column.

    Every sale line repeats the stock figure as it was at that moment, so adding the rows
    up would be wrong. The figure on each product's most recent row (by sale date) is the
    current stock, and the date of that row becomes the count date.
    """
    rows = pd.DataFrame({
        "product_id": product_ids,
        "sale_date": dates,
        "on_hand": _number(table, frame, "on_hand"),
        "reorder_point": _number(table, frame, "reorder_point"),
        "product_name": _text(table, frame, "product_name"),
    })
    rows = rows[rows["on_hand"].notna()]                 # only rows that state a stock figure
    latest = _latest_per_product(rows).reset_index()
    if len(latest):
        steps.append(("inventory_from_sales", "inventory", len(latest),
                      f"Stock levels were read from the sales sheet {table['name']}: for each product, "
                      "the on-hand figure on its most recent row (by sale date) was used."))
    return latest.rename(columns={"sale_date": "last_counted"})


def _count_date(table: dict, frame: pd.DataFrame) -> pd.Series:
    """The date of the stock count. There is no role for it, so the rule is: the first date
    column without a role, in a pure stock sheet; in a product sheet only when its header
    says so (counted, stocktake, updated, checked)."""
    for column in table["columns"]:
        if column["dtype"] != "date" or column.get("role"):
            continue
        tokens = set(_header_key(table, column["name"]).split("_"))
        if table["kind"] == "inventory" or tokens & COUNT_DATE_WORDS:
            return _day_only(pd.Series(frame[column["name"]].to_numpy(dtype=object, na_value=None), dtype=object))
    return pd.Series([None] * len(frame), dtype=object)


def _day_only(dates: pd.Series) -> pd.Series:
    """ "2024-01-05 14:30:00" -> "2024-01-05". The standard tables hold days, not times."""
    days = [None if value is None else str(value)[:10] for value in dates.to_numpy(dtype=object, na_value=None).tolist()]
    return pd.Series(days, dtype=object)


def _sale_lines(table: dict, frame: pd.DataFrame, product_ids: pd.Series, dates: pd.Series,
                supplier_ids) -> pd.DataFrame:
    order_ids = _id_text(table, frame, "order_id")
    return pd.DataFrame({
        "product_id": product_ids,
        "product_name": _text(table, frame, "product_name"),
        "category": _text(table, frame, "category"),
        "sale_date": dates,
        "quantity": _number(table, frame, "quantity"),
        "unit_price": _number(table, frame, "unit_price"),
        "unit_cost": _number(table, frame, "unit_cost"),
        "supplier_id": pd.Series(supplier_ids, dtype=object),      # product facts that a sales
        "moq": _number(table, frame, "moq"),                       # sheet may repeat on each line
        "revenue": _number(table, frame, "revenue"),
        "discount_pct": _number(table, frame, "discount_pct"),
        "channel": _text(table, frame, "channel"),
        "order_id": pd.Series(order_ids if order_ids is not None else [None] * len(frame), dtype=object),
    })


# --------------------------------------------------------------------------------------
# sales
# --------------------------------------------------------------------------------------
def _finish_sales(parts: list, steps: list) -> tuple:
    """Clean the sale lines. Returns (sales, catalog); catalog holds, per product, the
    latest name, category, price, cost, supplier and minimum order seen in the sales."""
    if not parts:
        empty = pd.DataFrame({name: pd.Series([], dtype=object) for name in SALES_COLUMNS})
        return empty, pd.DataFrame(columns=PRODUCT_COLUMNS[1:])
    sales = pd.concat(parts, ignore_index=True)

    incomplete = sales["product_id"].isna() | sales["sale_date"].isna() | sales["quantity"].isna()
    if incomplete.any():
        steps.append(("sales_rows_incomplete", "sales", int(incomplete.sum()),
                      "Sale rows without a product, a date or a quantity were left out."))
        sales = sales[~incomplete]

    # Quantities are whole units. Returns (negative) and zero lines are not sales.
    quantity = np.floor(sales["quantity"] + 0.5)
    fractional = int((quantity != sales["quantity"]).sum())
    if fractional:
        steps.append(("quantities_rounded", "sales", fractional,
                      "Quantities with a fraction were rounded to whole units."))
    returns, zero = int((quantity < 0).sum()), int((quantity == 0).sum())
    if returns:
        steps.append(("returns_excluded", "sales", returns, "Rows with a negative quantity (returns) were left out."))
    if zero:
        steps.append(("zero_quantity_excluded", "sales", zero, "Rows with a quantity of zero were left out."))
    sales = sales.assign(quantity=quantity)[quantity > 0].reset_index(drop=True)

    sales = _sale_prices(sales, steps)
    sales = _sale_discounts(sales, steps)
    sales = _sale_ids(sales, steps)

    # The catalog: for each product, the most recent value of each fact seen in the sales.
    facts = sales[["product_id", "sale_date", "product_name", "category", "unit_price_at_sale", "unit_cost",
                   "supplier_id", "moq"]].rename(columns={"unit_price_at_sale": "unit_price"})
    catalog = _latest_per_product(facts)[PRODUCT_COLUMNS[1:]]
    return sales, catalog


def _sale_prices(sales: pd.DataFrame, steps: list) -> pd.DataFrame:
    """The price of one unit on each sale line."""
    price, revenue = sales["unit_price"], sales["revenue"]
    derive = price.isna() & revenue.notna()
    if derive.any():
        price = price.where(~derive, (revenue / sales["quantity"]).round(4))
        # Revenue is what was actually charged, so a discount is already inside it.
        sales = sales.assign(discount_pct=sales["discount_pct"].where(~derive, 0.0))
        steps.append(("derived_unit_price", "sales", int(derive.sum()),
                      "The unit price was worked out as revenue divided by quantity; "
                      "the discount on those rows was set to 0 because revenue already includes it."))
    negative = price < 0
    if negative.any():
        price = price.where(~negative)
        steps.append(("negative_prices_removed", "sales", int(negative.sum()),
                      "Negative unit prices were left empty."))
    return sales.assign(unit_price_at_sale=price)


def _sale_discounts(sales: pd.DataFrame, steps: list) -> pd.DataFrame:
    """Discounts as a percentage between 0 and 100; empty means no discount."""
    discount = sales["discount_pct"]
    present = discount.dropna()
    if len(present) and present.max() <= 1 and (present % 1 != 0).any():
        discount = discount * 100           # 0.15 means 15 percent (Excel stores percentages this way)
        steps.append(("discount_scaled", "sales", int(len(present)),
                      "Discounts were written as fractions (0.15); they were multiplied by 100."))
    outside = (discount < 0) | (discount > 100)
    if outside.any():
        discount = discount.where(~outside, 0.0)
        steps.append(("discount_out_of_range", "sales", int(outside.sum()),
                      "Discounts below 0 or above 100 percent were set to 0."))
    return sales.assign(discount_pct=discount.fillna(0.0))


def _sale_ids(sales: pd.DataFrame, steps: list) -> pd.DataFrame:
    """Give every sale line a unique sale_id. No row is dropped here.

    * No order id column: ids SALE-0000001, SALE-0000002, ... are made up.
    * An order id that is different on every row is used as it is.
    * An order id that repeats is an order (an invoice) with several lines. Every sale_id
      then becomes the order id plus the line's place in the order: 536365-1, 536365-2, ...
      Rows that were exact copies of each other were already removed during cleaning, so
      two rows with the same order id here really are two different lines.
    """
    filled = sales["order_id"].notna()
    if not filled.any():
        steps.append(("derived_sale_ids", "sales", len(sales),
                      "There is no order id column; sale ids SALE-0000001, ... were made up."))
        return sales.assign(sale_id=[f"SALE-{number:07d}" for number in range(1, len(sales) + 1)])

    # Rows without an order id get a made-up one, so that every row belongs to an order.
    orders = [value if has_id else f"SALE-{row + 1:07d}"
              for row, (value, has_id) in enumerate(zip(sales["order_id"].tolist(), filled.tolist()))]
    sales = sales.assign(order_id=pd.Series(orders, dtype=object))
    if len(set(orders)) == len(orders):
        ids = orders
    else:
        line_numbers = (sales.groupby("order_id", sort=False).cumcount() + 1).tolist()
        ids = [f"{order_id}-{number}" for order_id, number in zip(orders, line_numbers)]
        steps.append(("derived_sale_ids", "sales", len(ids),
                      "The order id repeats across lines, so each sale id is the order id plus a line number."))

    # Last safety net: a sale_id must be unique whatever the file held ("A-1" may already
    # exist as an order id next to order "A").
    seen = set()
    for row, sale_id in enumerate(ids):
        if sale_id in seen:
            sale_id = f"{sale_id}#{row + 1}"
            ids[row] = sale_id
        seen.add(sale_id)
    return sales.assign(sale_id=pd.Series(ids, dtype=object))


def _fill_sale_prices(sales: pd.DataFrame, products: pd.DataFrame, sale_tables: list, steps: list) -> pd.DataFrame:
    """When the sales sheet has no price and no revenue at all, use the product list price."""
    has_price_column = any({"unit_price", "revenue"} & set(roles_of(table["columns"])) for table in sale_tables)
    if len(sales) == 0 or has_price_column:
        return sales
    list_price = products.set_index("product_id")["unit_price"]
    filled = sales["product_id"].map(list_price).astype("float64")
    if filled.notna().any():
        steps.append(("sale_prices_from_list_price", "sales", int(filled.notna().sum()),
                      "The sales sheet has no price column; each sale line uses the product's list price."))
    return sales.assign(unit_price_at_sale=filled)


# --------------------------------------------------------------------------------------
# products, inventory, suppliers
# --------------------------------------------------------------------------------------
def _finish_products(parts: list, catalog: pd.DataFrame, steps: list) -> pd.DataFrame:
    fact_columns = PRODUCT_COLUMNS[1:]
    if parts:
        # Repeats are counted inside each sheet: the same product in the product sheet and
        # in the stock sheet is expected, the same product twice in one sheet is not.
        repeats = sum(int(part["product_id"].dropna().duplicated().sum()) for part in parts)
        listed = pd.concat(parts, ignore_index=True)
        listed = listed[listed["product_id"].notna()]
        if repeats:
            steps.append(("product_rows_merged", "products", repeats,
                          "A product appears on more than one row; for each fact the value on the "
                          "lowest row that has one was kept."))
        listed = _latest_per_product(listed)[fact_columns]
    else:
        listed = pd.DataFrame(columns=fact_columns)

    # Complete the product list from the sales: add products that were sold but not listed,
    # and fill any fact the list leaves empty (name, category, price, cost, supplier, minimum
    # order) with the latest one seen in the sales.
    only_in_sales = catalog.index.difference(listed.index)
    all_ids = listed.index.append(only_in_sales)
    products = listed.reindex(all_ids)
    from_sales = catalog.reindex(all_ids)
    if len(only_in_sales):
        detail = "The product list was built from the sales rows (latest name and price per product)." \
            if not parts else "Products that are sold but missing from the product list were added from the sales rows."
        steps.append(("products_from_sales", "products", len(only_in_sales), detail))
    listed_without_price = products["unit_price"].isna() & from_sales["unit_price"].notna() \
        & ~pd.Series(products.index.isin(only_in_sales), index=products.index)
    if listed_without_price.any():
        steps.append(("list_price_from_sales", "products", int(listed_without_price.sum()),
                      "Products with no list price were given their latest sale price."))
    for column in fact_columns:
        products[column] = products[column].where(products[column].notna(), from_sales[column])

    products = products.reset_index(names="product_id")
    nameless = products["product_name"].isna()
    if nameless.any():
        products["product_name"] = products["product_name"].where(~nameless, products["product_id"])
        steps.append(("products_without_name", "products", int(nameless.sum()),
                      "Products with no name are shown by their id."))
    for column in ("unit_price", "unit_cost"):
        numbers = products[column].astype("float64")
        negative = numbers < 0
        if negative.any():
            steps.append(("negative_prices_removed", "products", int(negative.sum()),
                          f"Negative values in {column} were left empty."))
        products[column] = numbers.where(~negative)
    moq = np.floor(products["moq"].astype("float64") + 0.5)
    products["moq"] = moq.where(moq >= 1, 1.0)           # the minimum order is at least 1
    return products[PRODUCT_COLUMNS]


def _finish_inventory(sheet_parts: list, sales_parts: list, steps: list) -> tuple:
    """Build the inventory table. Returns (inventory, names), where names gives the product
    name for stock that was read from a sales sheet (used to add a product that has stock
    but no usable sale).

    Stock sheets come first: several rows for one product are several places where it is
    kept, so they are added up. Stock read from sales sheets (see _stock_from_sales) is
    used only for products that no stock sheet covers.
    """
    columns = INVENTORY_COLUMNS + ["product_name"]
    pieces = []
    if sheet_parts:
        stock = pd.concat(sheet_parts, ignore_index=True)
        unusable = stock["product_id"].isna() | stock["on_hand"].isna()
        if unusable.any():
            steps.append(("inventory_rows_incomplete", "inventory", int(unusable.sum()),
                          "Stock rows without a product or without an on-hand figure were left out."))
            stock = stock[~unusable]
        repeats = int(stock["product_id"].duplicated().sum())
        if repeats:
            steps.append(("inventory_rows_combined", "inventory", repeats,
                          "A product has more than one stock row; the on-hand figures were added together."))
        grouped = stock.groupby("product_id", sort=False)
        from_sheets = pd.DataFrame({
            "on_hand": grouped["on_hand"].sum(),
            "reorder_point": grouped["reorder_point"].max(),
            "last_counted": grouped["last_counted"].last(),
        }).reset_index(names="product_id")
        from_sheets["product_name"] = None            # these products are already in the product list
        pieces.append(from_sheets[columns])
    if sales_parts:
        # Two sales sheets may both state a product's stock: the more recent row wins.
        from_sales = pd.concat(sales_parts, ignore_index=True).rename(columns={"last_counted": "sale_date"})
        from_sales = _latest_per_product(from_sales).reset_index().rename(columns={"sale_date": "last_counted"})
        if pieces:
            from_sales = from_sales[~from_sales["product_id"].isin(pieces[0]["product_id"])]
        pieces.append(from_sales[columns])
    if not pieces:
        empty = pd.DataFrame({name: pd.Series([], dtype=object) for name in INVENTORY_COLUMNS})
        return empty, pd.Series([], dtype=object)

    inventory = pd.concat(pieces, ignore_index=True)
    on_hand = np.floor(inventory["on_hand"].astype("float64") + 0.5)
    negative = on_hand < 0
    if negative.any():
        steps.append(("negative_stock_set_to_zero", "inventory", int(negative.sum()),
                      "Negative on-hand figures were set to 0."))
    inventory["on_hand"] = on_hand.where(~negative, 0.0)
    reorder = np.floor(inventory["reorder_point"].astype("float64") + 0.5)
    inventory["reorder_point"] = reorder.where(reorder >= 0, 0.0)          # empty or negative -> 0
    names = inventory.set_index("product_id")["product_name"]
    return inventory[INVENTORY_COLUMNS], names


def _add_products_known_only_from_stock(products: pd.DataFrame, inventory: pd.DataFrame, names: pd.Series,
                                        steps: list) -> pd.DataFrame:
    """Every product with a stock level must also be in the products table. A product can
    have stock on a sales sheet without a single usable sale (only returns, or a quantity
    of zero); such a product is added here, with its name and nothing else."""
    missing = inventory.loc[~inventory["product_id"].isin(products["product_id"]), "product_id"].tolist()
    if not missing:
        return products
    steps.append(("products_from_stock_rows", "products", len(missing),
                  "Products that have a stock level but no usable sale were added to the product list."))
    known_names = names.dropna().to_dict()
    added = pd.DataFrame({
        "product_id": pd.Series(missing, dtype=object),
        "product_name": pd.Series([known_names.get(product, product) for product in missing], dtype=object),
        "category": pd.Series([None] * len(missing), dtype=object),
        "unit_price": np.nan, "unit_cost": np.nan,
        "supplier_id": pd.Series([None] * len(missing), dtype=object),
        "moq": 1.0,
    })
    return pd.concat([products, added], ignore_index=True)[PRODUCT_COLUMNS]


def _finish_suppliers(parts: list, steps: list) -> pd.DataFrame:
    if not parts:
        return pd.DataFrame({name: pd.Series([], dtype=object) for name in SUPPLIER_COLUMNS})
    listed = pd.concat(parts, ignore_index=True)
    listed = listed[listed["supplier_id"].notna()]
    grouped = listed.groupby("supplier_id", sort=False)
    # The suppliers table holds one lead time per supplier. When a supplier has different
    # lead times for different products, the longest is kept: planning with the longest
    # wait never leaves a product short.
    differing = int((grouped["lead_time_days"].nunique() > 1).sum())
    if differing:
        steps.append(("supplier_lead_times_differ", "suppliers", differing,
                      "A supplier has different lead times for different products or rows; the longest was kept."))
    suppliers = pd.DataFrame({
        "supplier_name": grouped["supplier_name"].first(),
        "lead_time_days": grouped["lead_time_days"].max(),
        "payment_terms": grouped["payment_terms"].first(),
    }).reset_index(names="supplier_id")
    return suppliers[SUPPLIER_COLUMNS]
