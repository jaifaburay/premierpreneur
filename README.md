# PremierPreneur — Milestone 1

Multi-agent business decision assistant for small online retailers (CIS 4394 – Agentic AI, Fall 2026).
Milestone 1 delivers **one agent running end to end** over **real calculation tools** with a
**max-iteration stop guard** and a **full execution trace** of every run.

```
premierpreneur/
├── run_agent.py              # CLI entry point
├── premierpreneur/
│   ├── agent.py              # the agent loop (reasoning → tool call → observation → answer)
│   ├── tools.py              # 5 deterministic tools over SQLite + their JSON schemas
│   ├── llm.py                # backends: openai | ollama | mock
│   ├── trace.py              # trace recorder → traces/*.md and *.json
│   └── db.py                 # CSV → SQLite loader (de-duplicates, flags missing costs)
├── data/
│   ├── generate_data.py      # seeded synthetic store data (no customer data)
│   ├── products.csv  sales.csv  inventory.csv  suppliers.csv
│   └── premierpreneur.db     # built automatically on first run
├── traces/                   # one .md + .json per run
└── docs/MILESTONE1_writeup.md
```

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
# 1) offline demo — no API key, scripted model, exercises the whole loop
python run_agent.py --backend mock --scenario restock

# 2) real model (OpenAI) — this is the Milestone 1 submission run
export OPENAI_API_KEY=sk-...            # Windows PowerShell: $env:OPENAI_API_KEY="sk-..."
python run_agent.py --backend openai --model gpt-4o-mini --scenario restock

# 3) local model through Ollama (OpenAI-compatible endpoint, needs a tool-calling model)
ollama pull llama3.1:8b
python run_agent.py --backend ollama --model llama3.1:8b --scenario restock

# ask anything
python run_agent.py --backend openai -q "I have $1,500 to restock. Be conservative. What should I order?"

# show the stop guard firing (loop cut off after 3 iterations, no answer issued)
python run_agent.py --backend mock --scenario restock --max-iterations 3

# other built-in questions
python run_agent.py --backend openai --scenario discount
python run_agent.py --backend openai --scenario restock-conservative

# wipe and regenerate the synthetic data + database
python run_agent.py --rebuild-data
```

Every run writes `traces/trace_<run-id>_<backend>.md` (readable) and `.json` (complete).
Exit code is `0` when the agent reached a final answer, `2` when the guard or an error stopped it.

## How the agent works

```
system prompt + owner question
        │
        ▼
┌─ iteration i of max_iterations ──────────────────────────────┐
│  model writes 1–2 sentences of reasoning                     │
│  ├─ tool call? ──► dispatch() runs Python over SQLite        │
│  │                 observation (JSON, or {"error": ...})     │
│  │                 appended as a tool message ──► loop       │
│  └─ no tool call ──► final answer  (stop_reason=final_answer)│
└──────────────────────────────────────────────────────────────┘
        │  loop exhausted
        ▼
stop_reason=max_iterations — partial evidence kept, no recommendation issued
```

The model never computes a number. The five tools are plain Python with input validation; bad
arguments come back as an observation (`{"error": "..."}`) so the model can correct itself instead
of the run crashing.

| Tool | What it returns |
|---|---|
| `get_sales_summary(days)` | units, net revenue, COGS, gross profit, margin %, prior-period trend per product; flags missing costs |
| `get_inventory_status()` | on-hand, 30-day velocity, days of cover, status `stockout_risk / low / ok / overstock` |
| `get_supplier_terms(product_ids?)` | unit cost, MOQ, supplier, lead time |
| `plan_restock(budget, strategy)` | two-pass budget allocation (survive lead time → top up to target), unfunded gap, assumptions |
| `evaluate_discount(product_id, discount_pct)` | margin at the discount, breakeven volume to hold gross profit |

## Synthetic data

`data/generate_data.py` builds a 12-SKU home-goods store with 90 days of order lines (seeded, so
every rebuild is identical). It deliberately includes the edge cases from the proposal's evaluation
plan: a product with **no unit cost** (P011), a **slow mover** (P010), a **fast mover at stockout risk**
(P002), **declining demand** (P012) and **duplicate sale rows** that the loader must drop.

## Roadmap (Milestone 2)

Planner / Decision Agent / Reviewer wrapping this loop, retry-with-feedback when the Reviewer rejects,
Streamlit workspace, Ollama vs OpenAI comparison, 20–30 case evaluation set with pass^k.
