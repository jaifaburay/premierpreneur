"""
Working out what each column means.

A cleaned table has columns such as "stock_code" or "qty_sold". The tools need to know which
column is the product id, which is the quantity, and so on. Each meaning is called a ROLE
(the list is in the build spec, section 3.3).

How a role is found for a column:

  1. The header. Every role has a list of header names it is known by. An exact match
     ("qty" for quantity) scores 0.95; a header that contains a known phrase as whole words
     ("total_qty_sold") scores 0.75; a short list of vague names ("total", "type") scores
     0.60. Words in a role's "avoid" list cancel the last two kinds of match, so
     "stock_code" is never taken for stock on hand.
  2. The values. A date role needs a date column, a quantity needs numbers, a discount
     needs numbers between 0 and 100, and so on. A column that fails the check cannot have
     the role, whatever its header says.
  3. Within one table each role goes to one column and each column gets one role: the
     highest scores are handed out first.

The result for every column is a role (or none), a confidence between 0 and 1, and up to
three alternatives, so the owner can correct a wrong guess before the data is saved.
"""
from __future__ import annotations

import pandas as pd

from .textclean import normalise_header

ROLES = (
    "product_id", "product_name", "category", "sale_date", "quantity", "unit_price", "unit_cost",
    "revenue", "discount_pct", "channel", "order_id", "customer_id", "on_hand", "reorder_point",
    "supplier_id", "supplier_name", "lead_time_days", "moq", "payment_terms",
)

EXACT_SCORE = 0.95
CONTAINS_SCORE = 0.75
WEAK_SCORE = 0.60
BARE_SCORE = 0.50          # a header that is only "id" or "name", decided by its neighbours
MINIMUM_SCORE = 0.40

NUMERIC = ("integer", "number")


def _words(text: str) -> list:
    return text.split()


# For each role: header names that mean it. All names are in the normalised form produced
# by textclean.normalise_header (lower case, words joined by underscores).
SYNONYMS = {
    "product_id": {
        "exact": _words("product_id productid prod_id sku sku_id sku_code item_id itemid item_code item_no "
                        "item_number product_code product_no product_number stock_code stockcode part_number "
                        "part_no part_id upc barcode ean asin article_no article_number variant_sku lineitem_sku"),
        "contains": _words("product_id item_id sku stock_code product_code item_code upc barcode"),
        "avoid": _words("supplier vendor customer order invoice"),
    },
    "product_name": {
        "exact": _words("product_name product item_name item description product_description item_description "
                        "product_title title lineitem_name article goods product_desc item_desc desc"),
        "contains": _words("product_name item_name description product_title"),
        "avoid": _words("supplier vendor customer category channel payment"),
    },
    "category": {
        "exact": _words("category product_category item_category categories department dept product_type "
                        "item_type subcategory sub_category product_line product_group item_group product_class "
                        "family collection segment"),
        "contains": _words("category department product_type"),
        "weak": _words("type group class"),
    },
    "sale_date": {
        "exact": _words("sale_date sales_date date order_date invoice_date transaction_date trans_date txn_date "
                        "purchase_date sold_date date_sold date_of_sale order_datetime invoice_datetime timestamp "
                        "datetime created_at sold_at paid_at day order_time transaction_time"),
        "contains": _words("sale_date order_date invoice_date transaction_date date"),
        "avoid": _words("counted count expiry expiration expire ship shipped shipping due delivery delivered "
                        "birth updated modified received restock restocked start end join joined signup last added"),
    },
    "quantity": {
        "exact": _words("quantity qty units units_sold qty_sold quantity_sold sold_qty sold_quantity "
                        "quantity_ordered qty_ordered order_quantity order_qty units_ordered sold pieces pcs "
                        "lineitem_quantity sales_qty sales_units sales_quantity unit_sales item_quantity "
                        "no_of_units number_of_units no_of_items item_count unit_count sales_volume"),
        "contains": _words("quantity qty units_sold units"),
        "weak": _words("count volume"),
        "avoid": _words("on hand stock reorder min minimum max maximum moq available inventory per price cost "
                        "safety level threshold remaining"),
    },
    "unit_price": {
        "exact": _words("unit_price unitprice price selling_price sell_price sale_price sales_price retail_price "
                        "list_price price_each unit_price_at_sale item_price price_per_unit unit_selling_price "
                        "lineitem_price mrp msrp unit_retail product_price retail each"),
        "contains": _words("unit_price selling_price sale_price retail_price list_price price"),
        "weak": _words("rate"),
        "avoid": _words("cost total purchase buy buying wholesale extended ext supplier vendor landed"),
    },
    "unit_cost": {
        "exact": _words("unit_cost unitcost cost cost_price purchase_price buy_price buying_price wholesale_price "
                        "cost_per_unit item_cost landed_cost standard_cost supplier_price vendor_price cost_each "
                        "product_cost wholesale_cost purchase_cost"),
        "contains": _words("unit_cost cost_price purchase_price wholesale_price cost"),
        "avoid": _words("total shipping freight delivery extended ext labor labour"),
    },
    "revenue": {
        "exact": _words("revenue line_total total_price total_sales sales_amount net_sales gross_sales "
                        "extended_price ext_price total_amount net_revenue line_amount sale_amount total_revenue "
                        "order_total item_total net_amount turnover sales_revenue sales_value invoice_amount "
                        "line_revenue total_sale sales_total gross_revenue amount_paid"),
        "contains": _words("revenue total_sales line_total sales_amount total_price extended_price"),
        "weak": _words("total amount sales value"),
        "avoid": _words("qty quantity units cost tax vat discount shipping freight profit stock margin"),
    },
    "discount_pct": {
        "exact": _words("discount_pct discount discount_percent discount_percentage disc disc_pct pct_discount "
                        "discount_rate percent_off pct_off markdown markdown_pct discount_applied"),
        "contains": _words("discount_pct discount_percent discount"),
        "avoid": _words("amount amt value code name total price"),
    },
    "channel": {
        "exact": _words("channel sales_channel order_channel source order_source platform store store_name "
                        "outlet marketplace branch shop location store_location"),
        "contains": _words("channel"),
        "avoid": _words("id"),
    },
    "order_id": {
        "exact": _words("order_id orderid order_no order_number invoice_no invoiceno invoice_number invoice "
                        "invoice_id transaction_id trans_id txn_id sale_id sales_id receipt receipt_no "
                        "receipt_number receipt_id order ticket ticket_no bill_no sales_order so_number order_ref "
                        "line_id transaction_no transaction_number"),
        "contains": _words("order_id invoice_no invoice_number invoice_id transaction_id sale_id order_no "
                           "order_number receipt"),
        "avoid": _words("date time customer supplier status total amount qty quantity"),
    },
    "customer_id": {
        "exact": _words("customer_id customerid customer cust_id client_id customer_no customer_number member_id "
                        "client buyer buyer_id user_id account_id customer_name customer_code"),
        "contains": _words("customer_id customer client_id"),
        "avoid": _words("date count"),
    },
    "on_hand": {
        "exact": _words("on_hand onhand stock in_stock instock qty_on_hand quantity_on_hand stock_on_hand "
                        "stock_level stock_qty stock_quantity inventory current_stock available qty_available "
                        "available_qty quantity_available available_quantity units_in_stock inventory_level "
                        "inventory_qty inventory_quantity soh closing_stock units_on_hand stock_units stock_count "
                        "available_stock quantity_in_stock qty_in_stock current_inventory inventory_on_hand "
                        "stock_available"),
        "contains": _words("on_hand in_stock stock_level stock_on_hand units_in_stock stock inventory"),
        "avoid": _words("reorder min minimum max maximum safety date value code status days out id threshold "
                        "cost price"),
    },
    "reorder_point": {
        "exact": _words("reorder_point reorder_level reorder min_stock minimum_stock rop safety_stock par_level "
                        "par min_level reorder_threshold low_stock_threshold restock_level minimum_level "
                        "reorder_at min_stock_level"),
        "contains": _words("reorder_point reorder_level safety_stock min_stock reorder"),
        "avoid": _words("qty quantity date"),
    },
    "supplier_id": {
        "exact": _words("supplier_id supplierid vendor_id vendorid supplier_code vendor_code supplier_no "
                        "vendor_no supplier_number vendor_number"),
        "contains": _words("supplier_id vendor_id"),
    },
    "supplier_name": {
        "exact": _words("supplier_name supplier vendor vendor_name manufacturer distributor wholesaler supplied_by"),
        "contains": _words("supplier_name vendor_name supplier vendor"),
        "avoid": _words("id code no number lead price terms email phone contact address cost"),
    },
    "lead_time_days": {
        "exact": _words("lead_time_days lead_time leadtime lead_days lead_time_in_days delivery_days "
                        "delivery_time days_to_deliver supplier_lead_time replenishment_days lead "
                        "lead_time_weeks lead_time_wks"),
        "contains": _words("lead_time leadtime lead_days delivery_days"),
    },
    "moq": {
        "exact": _words("moq min_order_qty min_order_quantity minimum_order_quantity minimum_order_qty "
                        "min_order minimum_order min_qty minimum_qty"),
        "contains": _words("moq min_order minimum_order"),
    },
    "payment_terms": {
        "exact": _words("payment_terms terms payment_term pay_terms credit_terms payment"),
        "contains": _words("payment_terms terms"),
        "avoid": _words("date amount"),
    },
}

# When two tables both have a column for a role, the summary mapping prefers the table of
# this kind (the product list for product facts, the sales sheet for sale facts, ...).
HOME_KIND = {
    "product_id": "products", "product_name": "products", "category": "products",
    "unit_cost": "products", "moq": "products",
    "sale_date": "transactions", "quantity": "transactions", "unit_price": "transactions",
    "revenue": "transactions", "discount_pct": "transactions", "channel": "transactions",
    "order_id": "transactions", "customer_id": "transactions",
    "on_hand": "inventory", "reorder_point": "inventory",
    "supplier_id": "suppliers", "supplier_name": "suppliers", "lead_time_days": "suppliers",
    "payment_terms": "suppliers",
}


# --------------------------------------------------------------------------------------
# Scoring one header against one role
# --------------------------------------------------------------------------------------
def header_score(key: str, role: str) -> float:
    """How strongly a normalised header points at a role: 0.95, 0.75, 0.60 or 0."""
    names = SYNONYMS[role]
    if key in names["exact"]:
        return EXACT_SCORE
    tokens = key.split("_")
    if any(token in names.get("avoid", ()) for token in tokens):
        return 0.0
    if key in names.get("weak", ()):
        return WEAK_SCORE
    padded = f"_{key}_"
    for phrase in names.get("contains", ()):
        if f"_{phrase}_" in padded:               # the phrase appears as whole words
            return CONTAINS_SCORE
    return 0.0


def value_check(role: str, dtype: str, series: pd.Series) -> bool:
    """Can a column with this type and these values hold this role?"""
    if role == "sale_date":
        return dtype == "date"
    if role in ("quantity", "unit_price", "unit_cost", "revenue", "on_hand", "reorder_point", "moq"):
        return dtype in NUMERIC
    if role in ("discount_pct", "lead_time_days"):
        if dtype not in NUMERIC:
            return False
        low, high = series.min(), series.max()
        if pd.isna(low):
            return False
        limit = 100 if role == "discount_pct" else 730
        return float(low) >= 0 and float(high) <= limit
    if role in ("product_name", "supplier_name", "payment_terms"):
        return dtype == "text"
    if role in ("category", "channel"):
        return dtype in ("text", "integer")
    # The id roles: codes are text or whole numbers, never dates, yes/no or fractions.
    return dtype in ("text", "integer")


def _bare_header_role(key: str, keys: list, dtypes: list):
    """A header that is only "id" or only "name" says nothing by itself; its neighbours do.

      id / code / no / number / ref / key
          in a sheet with a date column: the id of the sale     -> order_id
          otherwise:                      the id of the product -> product_id
      name
          in a sheet that has a supplier column and no product column -> supplier_name
          otherwise                                                    -> product_name
    """
    if key in ("id", "code", "no", "number", "ref", "key"):
        return "order_id" if "date" in dtypes else "product_id"
    if key == "name":
        has_supplier = any(header_score(other, "supplier_id") or other in ("supplier", "vendor") for other in keys)
        has_product = any(header_score(other, "product_id") >= EXACT_SCORE for other in keys)
        return "supplier_name" if has_supplier and not has_product else "product_name"
    return None


# --------------------------------------------------------------------------------------
# Roles for one table
# --------------------------------------------------------------------------------------
def detect_roles(columns: list, frame: pd.DataFrame) -> list:
    """Fill in "role", "confidence" and "alternatives" for every column of one table.

    `columns` is the list of column descriptions ({"name", "original_header", "dtype"});
    it is changed in place. Returns (column name, role, why) for every column whose header
    names a role but whose values do not fit it (a "Date" column that is not dates). Such
    a column gets no role, and the caller tells the owner why.
    """
    keys = [normalise_header(column["original_header"]) or column["name"] for column in columns]
    dtypes = [column["dtype"] for column in columns]
    row_count = max(len(frame), 1)

    # Every (column, role) pair that passes both the header test and the value test.
    scored = []                                     # (confidence, column index, role)
    rejected = {}                                   # column index -> (score, role) that failed on values
    for index, column in enumerate(columns):
        series = frame[column["name"]]
        filled_share = float(series.notna().sum()) / row_count
        candidates = {role: header_score(keys[index], role) for role in ROLES}
        bare = _bare_header_role(keys[index], keys, dtypes)
        if bare is not None:
            candidates[bare] = max(candidates[bare], BARE_SCORE)
        for role, score in candidates.items():
            if score <= 0:
                continue
            if not value_check(role, column["dtype"], series):
                if score >= CONTAINS_SCORE and score > rejected.get(index, (0, None))[0]:
                    rejected[index] = (score, role)
                continue
            # A half-empty column is a slightly weaker match than a full one.
            confidence = round(score * (0.85 + 0.15 * filled_share), 2)
            if confidence >= MINIMUM_SCORE:
                scored.append((confidence, index, role))

    # Hand out roles, best score first; earlier columns win ties.
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    chosen_role = {}                                # column index -> (role, confidence)
    taken_roles = set()
    for confidence, index, role in scored:
        if index not in chosen_role and role not in taken_roles:
            chosen_role[index] = (role, confidence)
            taken_roles.add(role)

    for index, column in enumerate(columns):
        role, confidence = chosen_role.get(index, (None, 0.0))
        others = [{"role": other_role, "confidence": other_confidence}
                  for other_confidence, other_index, other_role in scored
                  if other_index == index and other_role != role]
        column["role"] = role
        column["confidence"] = confidence
        column["alternatives"] = others[:3]

    return [(columns[index]["name"], role, _why_not(role, columns[index]["dtype"]))
            for index, (_score, role) in sorted(rejected.items()) if index not in chosen_role]


# How a few roles are called when talking to the owner; the others read well as they are
# once the underscores are gone ("unit cost", "sale date").
_ROLE_WORDS = {
    "on_hand": "stock on hand",
    "lead_time_days": "supplier lead time",
    "moq": "minimum order quantity",
    "discount_pct": "discount percent",
}


def role_words(role: str) -> str:
    """A role in owner words: "on_hand" -> "stock on hand", "unit_cost" -> "unit cost"."""
    return _ROLE_WORDS.get(role, role.replace("_", " "))


def role_named_by(header) -> str:
    """The role a header clearly names ("On Hand" -> "on_hand"), or "" when it names none.
    Used for columns that were dropped because they were empty: the owner should hear that
    the column was found but had nothing in it, not that it is missing."""
    key = normalise_header(header)
    best_score, best_role = max((header_score(key, role), role) for role in ROLES)
    return best_role if best_score >= CONTAINS_SCORE else ""


def _why_not(role: str, dtype: str) -> str:
    """Why a column failed value_check, in owner words."""
    if role == "sale_date":
        return "its values could not be read as dates"
    if role in ("discount_pct", "lead_time_days") and dtype in NUMERIC:
        return "its values are outside the range 0 to " + ("100" if role == "discount_pct" else "730")
    if role in ("quantity", "unit_price", "unit_cost", "revenue", "on_hand", "reorder_point", "moq",
                "discount_pct", "lead_time_days"):
        return "its values are not numbers"
    return f"its values are of type {dtype}"


def roles_of(columns: list) -> dict:
    """{role: column name} for one table."""
    return {column["role"]: column["name"] for column in columns if column.get("role")}


def table_kind(columns: list) -> str:
    """What a table is, judged by the roles its columns have.

      transactions  a product, a date and a quantity: one row per sale line
      inventory     a product and stock on hand, with no price, cost or category
      products      a product (with or without stock on hand)
      suppliers     a supplier and no product
      other         none of the above; still loaded and available to generic queries
    """
    roles = set(roles_of(columns))
    has_product = "product_id" in roles or "product_name" in roles
    if has_product and "sale_date" in roles and "quantity" in roles:
        return "transactions"
    if has_product:
        if "on_hand" in roles and not roles.intersection({"unit_price", "unit_cost", "category"}):
            return "inventory"
        return "products"
    if "supplier_id" in roles or "supplier_name" in roles:
        return "suppliers"
    return "other"


# --------------------------------------------------------------------------------------
# The summary mapping across all tables, and the owner's corrections
# --------------------------------------------------------------------------------------
def build_mapping(tables: list) -> dict:
    """{role: {"table", "column", "confidence", "alternatives"}}: the best column for each
    role over all tables. `alternatives` lists up to three other columns with the same role
    or that scored for it."""
    found = {}                                      # role -> list of (rank, entry)
    for table in tables:
        for column in table["columns"]:
            options = []
            if column.get("role"):
                options.append((column["role"], column["confidence"], True))
            options += [(alt["role"], alt["confidence"], False) for alt in column.get("alternatives", [])]
            for role, confidence, is_chosen in options:
                at_home = table["kind"] == HOME_KIND.get(role)
                entry = {"table": table["name"], "column": column["name"], "confidence": confidence}
                found.setdefault(role, []).append(((is_chosen, confidence, at_home), entry))

    mapping = {}
    for role in ROLES:
        ranked = sorted(found.get(role, []), key=lambda item: item[0], reverse=True)
        if not ranked or not ranked[0][0][0]:        # nobody actually holds this role
            continue
        best = dict(ranked[0][1])
        best["alternatives"] = [entry for _rank, entry in ranked[1:4]]
        mapping[role] = best
    return mapping


def apply_overrides(tables: list, frames: dict, overrides: dict) -> None:
    """Apply the owner's corrections to the roles, in place.

    overrides: {role: {"table": "t_...", "column": "<name>"}}  gives that column the role
               {role: None}                                      removes the role everywhere
    Raises ValueError with a plain message when a correction cannot be applied.
    """
    for role, target in (overrides or {}).items():
        if role not in ROLES:
            raise ValueError(f"'{role}' is not a known role. The roles are: {', '.join(ROLES)}.")
        if target is None:
            for table in tables:
                for column in table["columns"]:
                    if column.get("role") == role:
                        column.update(role=None, confidence=0.0)
            continue
        table, column = _find_column(tables, target)
        if not value_check(role, column["dtype"], frames[table["name"]][column["name"]]):
            raise ValueError(
                f"Column '{column['name']}' in {table['name']} holds {column['dtype']} values, "
                f"which cannot be used as {role}. Choose a different column."
            )
        for other in table["columns"]:
            if other.get("role") == role:
                other.update(role=None, confidence=0.0)
        column.update(role=role, confidence=1.0)
    for table in tables:
        table["kind"] = table_kind(table["columns"])


def _find_column(tables: list, target) -> tuple:
    if not isinstance(target, dict) or "column" not in target:
        raise ValueError("A mapping entry must look like {'table': 't_sales', 'column': 'qty'} or be None.")
    matches = [(table, column) for table in tables for column in table["columns"]
               if column["name"] == target["column"] and target.get("table") in (None, table["name"])]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"There is no column '{target['column']}' in table '{target.get('table')}'.")
    raise ValueError(f"Column '{target['column']}' exists in more than one table; say which table.")
