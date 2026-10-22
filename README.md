# PremierPreneur

This is a business decision assistant for small retailers, built for my CIS 4394 (Agentic AI) at Georgia State University's Robinson College of Business.
An AI agent answers an owner's question like *"I have $1,000 to restock this week, what should I order?"*
by calling calculation tools over the store's sales and inventory data. The model never does the math;
the tools do, and the model explains the result.

**Milestone 1 (this version):** Contains one agent, five tools, a max-iteration of 10 stop guard, and a saved execution trace.

## How it works

1. The owner's question goes to the model with a system prompt (its role, how to work, tool rules, and answer format).
2. The model either calls a tool or answers. If it calls a tool, Python runs it over the SQLite database and sends the results back.
3. This repeats until the model answers, or until the iteration limit stops it.
4. Every step (reasoning, tool call, result) is saved to a trace file.

## Files

| File | What it is |
|---|---|
| `my_agent.py` | The agent loop, stop guard, bounded retries and trace writer. Run this. |
| `my_tools.py` | The first tool I wrote (`get_inventory_status`); kept for reference. |
| `plan_tool.py` | An `update_plan` tool modelled on OpenAI's Codex harness. |
| `premierpreneur/tools.py` | The five calculation tools: sales summary, inventory status, supplier terms, restock planner, discount evaluator. |
| `premierpreneur/db.py` | Loads the CSVs into SQLite, drops duplicate sales, flags missing costs. |
| `data/` | Synthetic 12-product store with 90 days of sales. `generate_data.py` rebuilds it. |
| `traces/` | Saved runs. `trace_restock_qwen.json` is the Milestone 1 submission. |
| `hello.py` | One-line model test used during setup. |
| `run_agent.py`, `premierpreneur/agent.py`, `llm.py`, `trace.py` | A fuller runner with backend switching; Milestone 2 will build on it. |

## Models

- **Cloud:** Gemini 3.8 Flash on Google AI Studio's free tier (the course model).
- **Local:** Qwen3-8B, 4-bit (Q4_K_M), run with Ollama. The submitted trace used this one because Gemini was overloaded during the run window.

## How to run it (Run each line of code one by one)
pip install -r requirements.txt
python data\generate_data.py
python -m premierpreneur.db

Create a file named `.env` containing `GEMINI_API_KEY=your-key` (acts as your password, do not disclose it). 

Then: python my_agent.py

To use the local model instead, install Ollama, run `ollama pull qwen3:8b`, and in `my_agent.py` set
`client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")` and `MODEL = "qwen3:8b"`.

To see the stop guard fire, set `MAX_ITERATIONS = 1` and run again.

## AI use

Claude helped write the data generator, the five calculation tools and the trace format, and explained each
piece while I built the agent loop, plan tool and retry logic.
