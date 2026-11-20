# PremierPreneur Milestone 3 build spec

This file is the contract between the parts being built in parallel. If you are building one
part, read all of it, build only what you own, and keep to the interfaces written here. If an
interface cannot work as written, do not silently change it: build the closest thing and say
exactly what differs in your final report.

## 1. What the project is

PremierPreneur is a decision assistant for a small retailer (a class project, CIS 4394 Agentic AI).
The owner asks a question in a chat page; deterministic Python tools compute every number over
SQLite; a local model (Qwen3-8B through Ollama, 8,192-token context, thinking off) chooses tools
and explains results. Milestone 2 state, already in this repo:

- `agent_core.py`  Business Analyst loop, human approval gate for `save_purchase_order_draft`,
                   memory of the last 3 turns, caps, session logging.
- `reviewer.py`    second agent: number check (code), product check (code), two yes/no questions (model).
- `premierpreneur/tools.py`  five tools over four tables (products, sales, inventory, suppliers).
- `premierpreneur/db.py`     loads the synthetic 12-product store from CSVs in `data/`.
- `app.py`         Gradio chat page.  `eval_harness.py`, `eval_cases.json`, `check_reviewer.py`: evaluation.

## 2. What Milestone 3 must add (the owner's stated scope)

1. Anyone can submit **any Excel or CSV file, even unclean**. It is **cleaned in a separate step**
   before any agent sees it. Blank, unreadable or unusable files return **invalid** with a reason.
2. The system works on **any tabular business dataset**, not only the synthetic store, and on
   **much bigger datasets** (target: 1,000,000 sales rows, 20,000 products).
3. A **Planner agent** and a **Decision agent** exist as separate agents (the owner has decided
   this), alongside the Business Analyst and the Reviewer. Each is tested on its own.
4. A **security review** will be written about the result, so every guardrail must be real,
   in code, and covered by a test: untrusted inputs named, no exfiltration path, layered guards.

Non-negotiable design rules carried over from Milestones 1 and 2:

- The model never computes a number. Tools do.
- The model never writes SQL and never sees a file path. Tool arguments are validated in code.
- Text from files is data, never instructions. Cleaning involves no model at all.
- The agent never places orders, pays, or sends anything. The only "action" is saving an order
  draft, and only after the owner approves the exact lines.
- Tool results stay small enough for an 8,192-token context: aggregate in SQL, cap rows.
- The 14 existing evaluation cases on the synthetic store must keep passing unchanged.

## 3. The data contract (one SQLite file per dataset)

Every dataset, the built-in store included, lives in one SQLite file with two layers.

### 3.1 Generic layer (always present)

- One table per cleaned sheet, named `t_<slug>` (lowercase letters, digits, underscore; unique).
  Columns are the cleaned headers (snake_case, unique, max 40 chars, start with a letter).
  SQLite types: TEXT, INTEGER, REAL. Dates are TEXT in ISO `yyyy-mm-dd` (or `yyyy-mm-dd hh:mm:ss`).
- `_tables(table_name TEXT PRIMARY KEY, source_file TEXT, source_sheet TEXT, row_count INTEGER, kind TEXT)`
  `kind` is one of `transactions`, `products`, `inventory`, `suppliers`, `other`.
- `_columns(table_name TEXT, column_name TEXT, original_header TEXT, dtype TEXT, null_count INTEGER,
            distinct_count INTEGER, min_value TEXT, max_value TEXT, role TEXT,
            PRIMARY KEY (table_name, column_name))`
  `dtype` is one of `text`, `number`, `integer`, `date`, `bool`. `role` is a role from 3.3 or NULL.
- `_cleaning_report(step TEXT, table_name TEXT, count INTEGER, detail TEXT)`  one row per cleaning action.
- `_capabilities(name TEXT PRIMARY KEY, available INTEGER, reason TEXT)`  see 3.4.
- `_assumptions(name TEXT PRIMARY KEY, value TEXT)`  owner-supplied defaults, e.g.
  `default_lead_time_days`, `default_moq`. Empty unless the owner sets them.

### 3.2 Canonical layer (filled as far as the data allows)

The four tables the existing tools read, same names and column names as `premierpreneur/db.py`:

```
suppliers(supplier_id TEXT PRIMARY KEY, supplier_name TEXT, lead_time_days INTEGER, payment_terms TEXT)
products(product_id TEXT PRIMARY KEY, product_name TEXT NOT NULL, category TEXT, unit_price REAL,
         unit_cost REAL, supplier_id TEXT, moq INTEGER NOT NULL DEFAULT 1)
sales(sale_id TEXT PRIMARY KEY, sale_date TEXT NOT NULL, product_id TEXT NOT NULL, quantity INTEGER NOT NULL,
      unit_price_at_sale REAL, discount_pct REAL NOT NULL DEFAULT 0, channel TEXT)
inventory(product_id TEXT PRIMARY KEY, on_hand INTEGER NOT NULL, reorder_point INTEGER NOT NULL DEFAULT 0,
          last_counted TEXT)
```

Differences from Milestone 2 that tools must tolerate: `unit_price`, `unit_cost`, `category`,
`supplier_id`, `supplier_name`, `lead_time_days`, `unit_price_at_sale` may be NULL; any of the four
tables may be empty; there are no foreign-key constraints. `sales.quantity` is always > 0 (returns
and zero-quantity lines are excluded by the loader and counted in the cleaning report).
Indexes: `sales(sale_date)`, `sales(product_id)`.

A flat transactions sheet (one row per sale line with product, date, quantity, price) is enough to
fill `products` and `sales`. `inventory` and `suppliers` stay empty unless the file has them.

### 3.3 Column roles

`product_id, product_name, category, sale_date, quantity, unit_price, unit_cost, revenue,
discount_pct, channel, order_id, customer_id, on_hand, reorder_point, supplier_id, supplier_name,
lead_time_days, moq, payment_terms`

### 3.4 Capabilities

`_capabilities` rows, each with `available` 0/1 and a plain-English `reason` when 0:

| name | needs |
|---|---|
| `sales_analysis` | sales rows with product, date, quantity |
| `revenue` | `sales_analysis` plus a price for sales lines |
| `margin` | `revenue` plus `unit_cost` for at least one product |
| `inventory_status` | `sales_analysis` plus inventory rows |
| `supplier_terms` | lead time for at least one product (from suppliers or `_assumptions`) |
| `restock_planning` | `inventory_status` plus `unit_cost` for at least one product plus a lead time (as `supplier_terms`) |
| `discount_evaluation` | `margin` |

A tool whose capability is off raises `ToolError` with a message that names what is missing and what
the owner would need to add, for example: "This dataset has no stock levels, so I cannot say what is
running low. Add a column with units on hand for each product."

The built-in synthetic store must be expressible in this contract with every capability on.

## 4. Who owns what

| Part | Owner | Files (create or edit ONLY these) |
|---|---|---|
| A. Ingestion and cleaning | Agent A | `ingest/` (new package), `tests/test_ingest.py`, `tests/fixtures_ingest.py` |
| B. Tools at scale, generic queries, scenarios | Agent B | `premierpreneur/tools.py`, `tests/test_tools.py` |
| C. Planner, Decision agent, guards, Reviewer additions | Agent C | `planner.py`, `decision.py`, `guards.py`, `reviewer.py`, `check_planner.py`, `check_decision.py`, `check_reviewer.py`, `tests/test_agents.py`, `tests/fake_client.py` |
| Core wiring | lead (later) | `agent_core.py`, `premierpreneur/db.py` |
| App | later | `app.py` |
| Evaluation, second dataset, security cases | later | `eval_harness.py`, `eval_cases*.json`, `data/` |

Do not edit a file you do not own, even to fix a bug: report it instead. Do not run git commands
that change the repository (no add, commit, checkout, stash, reset). Reading with git is fine.

## 5. Part A: ingestion and cleaning (`ingest/`)

Public API, importable as `from ingest import prepare, commit, ingest_file, set_assumptions, summarize`:

```python
prepare(path, original_name=None, max_bytes=50_000_000, max_rows=2_000_000) -> dict
    # Reads and cleans. Writes nothing. Returns:
    # {"status": "ok" | "invalid",
    #  "reason_code": None | "unsupported_type" | "too_large" | "unreadable" | "password_protected"
    #                 | "empty_file" | "no_data" | "no_usable_table" | "too_many_rows",
    #  "reason": None | "<one plain sentence for the owner>",
    #  "tables": [{"name": "t_...", "source_sheet": str, "rows": int, "kind": str,
    #              "columns": [{"name", "original_header", "dtype", "role", "confidence", "alternatives"}]}],
    #  "report": [{"step": str, "table": str, "count": int, "detail": str}],
    #  "mapping": {"<role>": {"table": "t_...", "column": "<name>", "confidence": 0..1}},
    #  "warnings": [str],
    #  "_frames": {...}}        # internal: cleaned data kept for commit(); never shown or logged

commit(prepared, db_path, mapping=None, assumptions=None) -> dict
    # Writes the SQLite file described in section 3 (replacing it). `mapping` lets the owner
    # override roles: {"<role>": {"table": ..., "column": ...} | None}. Returns summarize(db_path).

ingest_file(path, db_path, original_name=None, mapping=None, assumptions=None) -> dict
    # prepare() then commit(). Returns {"status", "reason_code", "reason", "report", "summary"}.

set_assumptions(db_path, **values) -> dict      # e.g. default_lead_time_days=14, default_moq=1;
    # validates (integers, sane ranges), rewrites _assumptions and _capabilities, returns summarize().

summarize(db_path) -> dict
    # {"tables": [{"name", "rows", "kind", "columns": [{"name", "dtype", "role"}]}],
    #  "capabilities": {name: {"available": bool, "reason": str}},
    #  "canonical_counts": {"products": n, "sales": n, "inventory": n, "suppliers": n},
    #  "date_range": {"first": iso | None, "last": iso | None},
    #  "assumptions": {...}}
```

Also: `python -m ingest <file> [--db out.db]` prints the cleaning report and summary.

File types: `.xlsx`, `.xlsm` (values only; macros are never run), `.xls` (through `xlrd` if it is
installed, otherwise invalid with a reason that says how to convert it), `.csv`, `.tsv`, `.txt`
(sniff the delimiter; try utf-8, utf-8-sig, cp1252, latin-1). Decide the type from content as well
as the extension: a renamed file must not crash the reader. Every reader error becomes `invalid`
with a reason; `prepare` must never raise on a bad file.

Cleaning, each step counted in the report: find the real header row (titles and blank rows may sit
above it; headers may be merged or span two rows); drop fully blank rows and columns; drop repeated
header rows and total/subtotal rows; trim whitespace; normalise and de-duplicate header names;
infer each column's type and convert (numbers like `$1,234.50`, `(45.00)`, `12%`, `1.234,50`;
dates in common formats and Excel serial numbers; yes/no booleans); values that cannot be converted
become NULL and are counted; drop exact duplicate rows; drop all-null columns; cut text cells to
200 characters and strip control characters.

Untrusted text: cells and headers are data from a stranger. Never `eval`, never build SQL by string
concatenation with cell text, never follow links. Redact text that looks like an instruction to a
model or a link (for example "ignore previous instructions", "system prompt", "you are now",
`http://`, `https://`, markdown image or link syntax, HTML tags) by replacing the cell with
`[removed: instruction-like text]` or `[removed: link]`, and count each kind in the report. Headers
get the same treatment before they are normalised. Formula cells are read as their cached values;
text that begins with `=`, `+`, `-` or `@` and is not a number is kept as plain text.

Mapping: detect roles from header synonyms plus checks on the values (a date role must parse as
dates, a quantity must be numeric, and so on), with a confidence and up to three alternatives per
role. Derive what can be derived and say so in the report: unit price from revenue / quantity,
product ids from product names when there is no id column, a product catalog from a flat
transactions sheet (latest price per product), sale ids when there is no order id.

Scale: 1,000,000 rows of CSV in a few minutes and bounded memory (read in chunks, insert in
batches, one transaction per table). Write tests that build their own messy fixtures in code
(`tests/fixtures_ingest.py`): blank workbook, header-only sheet, title rows above the header,
merged headers, mixed types, currency strings, duplicate rows, a totals row, two sheets
(products + sales), a flat transactions file in the style of public retail datasets
(InvoiceNo, StockCode, Description, Quantity, InvoiceDate, UnitPrice, CustomerID, Country),
a corrupt file, a text file renamed `.xlsx`, a password-protected-looking file if feasible,
a sheet with instruction-like text and links in cells and headers, and a 300,000-row CSV for timing.

## 6. Part B: tools (`premierpreneur/tools.py`)

Keep `ToolError`, `TOOL_SCHEMAS`, `TOOL_FUNCTIONS`, `dispatch`, and the five existing tool names.
Every tool keeps the internal `db_path=None` keyword (never in a schema). Existing results on the
synthetic store must not change in any value the 14 evaluation cases check (see `eval_cases.json`);
adding fields is fine.

Make the five tools safe on the section 3 contract and on big data:

- Check `_capabilities` first when the table exists; when it does not (an old database built by
  `premierpreneur/db.py`), behave exactly as today.
- Tolerate NULL prices, costs, suppliers and empty tables. Report gaps; never invent a value.
  When a lead time or MOQ comes from `_assumptions`, mark that line as assumed.
- Bound every result. New optional arguments with defaults that return everything on the
  12-product store: `get_sales_summary(days=30, top_n=20, sort_by="net_revenue", product_ids=None)`,
  `get_inventory_status(status=None, top_n=20)`, `get_supplier_terms(product_ids=None, top_n=20)`.
  Always return store-wide totals and counts (how many products in each status, how many not shown).
  `plan_restock` returns at most 30 order lines plus totals.
- Aggregate in SQL. Target: each tool under 2 seconds on 1,000,000 sales rows and 20,000 products.

New tools (add schemas with clear descriptions; the model reads them):

```python
describe_data() -> dict
    # tables, columns (name, dtype, role), row counts, date range, capabilities with reasons.
    # At most 12 tables and 40 columns per table in the result; say how many were left out.

query_data(table, metrics, group_by=None, filters=None, order_by=None, descending=True,
           limit=20, date_bucket=None) -> dict
    # Safe aggregate query over ONE table from _tables (generic t_ tables or the canonical four).
    # metrics:   [{"column": str | "*", "agg": "sum"|"avg"|"min"|"max"|"count"|"count_distinct"}]  (1 to 5)
    # group_by:  [column, ...]  (0 to 3)
    # filters:   [{"column": str, "op": "="|"!="|">"|">="|"<"|"<="|"between"|"in"|"contains", "value": ...}] (0 to 6)
    # date_bucket: {"column": str, "by": "day"|"week"|"month"|"year"}  adds a bucket to group_by
    # limit: 1 to 50.  Every identifier is checked against the data dictionary; every value is a
    # bound parameter; the model never supplies SQL. Text values in results are cut to 80 characters.
    # Returns {"columns": [...], "rows": [[...]], "row_count": n, "truncated": bool, "notes": [...]}.

compare_scenarios(budget) -> dict
    # plan_restock for conservative, moderate and aggressive, side by side:
    # per scenario: total_cost, budget_left, units, order lines (top 10), products_covered,
    # stockout_risk_remaining, unfunded_need. Plus which numbers differ and by how much.

compare_periods(days=30, top_n=5) -> dict
    # the latest `days` against the `days` before: units, net revenue, gross profit (when margin
    # is available), each with change and percent change, and the top_n products driving the change.
```

Tests in `tests/test_tools.py` (plain script, exit code 0 on success): the synthetic store gives
the same key numbers as today (104 / 20 / 12 units and $999.20 for plan_restock(1000, "moderate");
P002 30-day gross margin 64.58; Cast Iron Skillet lead time 30 and MOQ 6; P010 at 30% discount
new price $13.29); every tool on a database with only products and sales (capability errors are
readable, sales tools work); `query_data` rejects unknown tables, columns, operators and anything
that looks like SQL; a generated 1,000,000-row database timed per tool.

## 7. Part C: Planner, Decision agent, guards, Reviewer additions

Model calls follow `reviewer.llm_review`: the function takes `client`, `model` and
`model_options`, asks for a fixed JSON shape with `response_format` (falling back when the server
refuses it), reads the JSON leniently, validates it in code, and returns a safe result when the
reply is unreadable. No function may raise because of a bad model reply.

```python
# planner.py - the Planner agent. Sees the question, the data summary and the tool list.
# It never sees tool results or the conversation, and it has no tools.
make_plan(client, model, question, data_summary, tool_catalog, model_options=None) -> dict
    # {"status": "ok" | "unreadable",
    #  "answerable": bool,            # False when the data lacks what the question needs
    #  "missing": [str],              # what is missing, in owner words
    #  "steps": [{"tool": "<a name from tool_catalog>", "why": "<short>"}],   # 1 to 6, validated
    #  "note": str}
plan_as_text(plan) -> str             # the short block handed to the Business Analyst

# decision.py - the Decision agent. Sees ONLY the compare_scenarios result and the owner's goal.
choose_scenario(client, model, comparison, owner_goal="", model_options=None) -> dict
    # {"status": "ok" | "unreadable", "recommended": "conservative"|"moderate"|"aggressive"|None,
    #  "reason": str, "tradeoffs": [str]}
    # Code checks: `recommended` is one of the three; every number in reason/tradeoffs appears in
    # `comparison` (reuse reviewer.check_numbers); otherwise status "unreadable" with why.

# guards.py - plain code, no model.
sanitize_answer(text) -> (clean_text, [what was removed])
    # Neutralise anything in a model answer that could send data out of the page or run code:
    # markdown images, markdown links, raw URLs, HTML tags. Keep the visible words.
check_complete(draft, observations) -> [issues]
    # A restock answer must state the plan's total cost and name every product the planner left
    # out for missing data. Generalise carefully: only fire when a tool result this turn contains
    # the fields in question.
```

`reviewer.py`: add `check_complete` as a code step after the product check, with a `detail` entry,
keeping the `review()` signature and return shape. Do not loosen any existing check.

`check_planner.py` and `check_decision.py`: same idea as `check_reviewer.py`. Labelled inputs,
3 runs each, a count of wrong plans / wrong choices / unreadable replies. They need a real model,
so also ship `tests/fake_client.py` (a scripted stand-in with the OpenAI client shape) and
`tests/test_agents.py`, which proves the plumbing and every code check without a model.

Each agent's module docstring must state its reason for being a separate agent in the terms the
course uses: context isolation, permission isolation, parallel breadth, or team ownership.

## 8. Rules for all parts

- Python 3.10+ syntax, standard library plus `pandas` (works on 2.x and 3.x; do not rely on
  object dtype for strings), `openpyxl`, optional `xlrd`. Nothing else. No network access.
- Must run on Windows: use `pathlib`, open text files with an explicit encoding, no shell calls.
- The student who owns this project must be able to explain every file. Write a module docstring
  that says what the file does and why, keep functions short, comment the non-obvious steps, and
  avoid metaprogramming and one-line cleverness. No emojis.
- Error messages are read by the owner or by the model: say what is wrong and what to do.
- There is no model available where you run. Anything that calls a model must be tested with a
  scripted stand-in, and your report must say plainly what has not been tested against a real model.
- Tests are plain scripts: `python tests/test_x.py` prints one line per check and exits non-zero
  on any failure. Do not add pytest.
- Run your tests before you report. Report failures as failures.

## 9. Final report format (your last message)

1. Files created and changed.
2. The public API exactly as built (signatures), and every deviation from this spec with the reason.
3. Tests run, with the command and the pass/fail counts, and timings where the spec set a target.
4. What is not done, not tested, or uncertain. Be specific.
5. Bugs or problems you noticed in files you do not own.
