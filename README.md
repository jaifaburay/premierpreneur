# PremierPreneur

This is a business decision assistant for small retailers, built for my CIS 4394 (Agentic AI) at Georgia State University's Robinson College of Business.
An AI agent answers an owner's question like *"I have $1,000 to restock this week, what should I order?"*
by calling calculation tools over the store's sales and inventory data. The model never does the math;
the tools do, and the model explains the result.

**Milestone 2 (this version):** Created a chat page with a public link, a second agent that reviews every answer, a human approval step before the one action the agent can take, and an evaluation harness that reports pass@1 and pass^k.

## Read this first: what this version is and is not
- It's only been tested on a made-up store of synthetic data found in data/. That store has 12 products and 90 days of sales, and I built the tools and the test cases around it. The results below say nothing about how it would do on a real business.
- It doesn't work with anyone else's data yet. There's no Excel file upload. The tools expect four tables in one fixed layout (products, sales, inventory, suppliers).
- It's only been evaluated on one model: Qwen3-8B (4-bit) running locally through Ollama, with thinking turned off. The code can also call Gemini, but I did not evaluate that in this milestone.
- It's a class project and is not ready to be used properly by anyone. Use it at your own discretion. Do not use its answers for any real purchasing decisions.


## How it works
1. The owner types a question on the chat page.
2. The Business Analyst agent either calls a tool or drafts an answer. Tools run in Python over a SQLite database, and the results go back to the model.
3. The Reviewer agent checks the draft before the owner sees it, using 3 main things:
   - a number check in code: every number must come from a tool result
   - a product check in code: the tools must have looked at the product the owner asked about
   - two yes/no questions answered by the model in a clean context: is the draft about what was asked, and does it claim something was ordered, paid or sent?
4. If the draft fails, it goes back to the Analyst with the exact problem, up to 2 times. After that, the answer is shown with a note that it was not fully verified.
5. If the Analyst wants to save an order draft, the loop stops and shows an approval card with the exact arguments and catalog prices. Nothing is saved unless the owner answers Yes. An approved draft can be downloaded as an Excel file. No supplier is contacted and nothing is bought.
6. Every step is saved to a session log on the laptop running the app.

## Evaluation results

14 test cases with known-good answers (8 normal, 3 edge, 3 adversarial), each run 3 times and graded by code.
pass@1 is the share of single runs that passed. pass^3 is the share of cases that passed all 3 runs.

| Temperature | Configuration | pass@1 | pass^3 | Avg seconds |
|---|---|---|---|---|
| 0 | Analyst + Reviewer | 100% | 100% | 5.2 |
| 0 | Analyst + Reviewer's code checks only | 100% | 100% | 4.3 |
| 0 | Analyst alone (baseline) | 93% | 93% | 4.1 |
| 0.7 | Analyst + Reviewer | 98% | 93% | 5.6 |
| 0.7 | Analyst + Reviewer's code checks only | 98% | 93% | 4.8 |
| 0.7 | Analyst alone (baseline) | 93% | 93% | 4.8 |

The first run of the night scored 86% for both the Analyst alone and the first version of the Reviewer.
Every run, including the early ones, is in `eval_results/`.

What the harness found:

- The agent answered about the wrong product when given a product name, because the tool only accepted IDs. Fixed in the tool.
- The agent repeated a false "95% margin" from the owner's message without checking it. The Reviewer's number check now sends that back. The Analyst alone still fails this case.
- The first Reviewer raised false alarms on correct answers and twice argued the Analyst into a wrong answer. I rebuilt it to ask two narrow questions and tested it on its own (`check_reviewer.py`: 0 false alarms on 18 good drafts, 0 misses on 12 bad ones).
- **Still open:** with varied wording, the long restock answer sometimes leaves out the total or a missing-cost note. Nothing checks for missing content yet.

## Dev notes: where this stands against the proposal

| From the proposal | Status in Milestone 2 | What is left |
|---|---|---|
| Business Analyst agent | Built. One loop, five calculation tools, memory of the last 3 turns, stop guard and caps. | Tools for questions outside restocking, margins and discounts. |
| Reviewer agent | Built and evaluated against the single-agent baseline. | A check for missing content. A test with thinking turned on. |
| Planner agent | Not a separate agent. The Analyst has an `update_plan` tool and is told to plan first. | Decide whether a separate agent is justified, then test it against the baseline. |
| Decision Agent and the three decision temperatures | Partly. The restock planner accepts conservative, moderate or aggressive, but one at a time. | A side-by-side comparison of the three, then a separate agent only if it earns its place. |
| Owner reviews everything, the agent never places orders | Built. Approval card, Excel draft, no tool that orders or pays. | Nothing for this milestone. |
| CSV uploads into SQLite | Not built. The CSVs in `data/` are loaded by a script. | Upload for CSV and Excel, cleaning, "invalid" for blank or unreadable files, protection against instructions hidden in cells. |
| Dashboard with metrics, inventory views and saved recommendations | Not built. This version is a chat page. | The whole dashboard. |
| Real-world use cases | Not started. Synthetic data only. | A public dataset as a second use case, with its own test cases. |

Added since the proposal and also not built: creating a starter dataset from an owner's spoken description when they have no records, and attaching OpenAI's Codex harness directly to Qwen (this version borrows the Codex design: a sectioned system prompt, a plan tool and bounded retries).

Other known limits:

- The product check can wrongly reject an answer if the owner misspells a product name.
- 14 test cases is a small set, and the known-good answers belong to this one store.
- Thinking is off for speed (about 15 seconds for a restock answer instead of about 190), which weakens the model's judgment.
- The public link runs on my laptop and only works while the app is running.

## Files

| File | What it is |
|---|---|
| `app.py` | The chat page (Gradio). Run this. |
| `agent_core.py` | The Business Analyst loop, the approval gate, memory, caps and session logging. |
| `reviewer.py` | The Reviewer: number check, product check and the model's two questions. |
| `check_reviewer.py` | Tests the Reviewer on its own with drafts whose correct verdict is known. |
| `eval_harness.py` | Runs every test case several times and reports pass@1 and pass^k. |
| `eval_cases.json` | The 14 test cases and their known-good answers. |
| `eval_results/` | Results of every evaluation run. |
| `check_m1_answer.py` | Runs the number check on a saved Milestone 1 trace. |
| `premierpreneur/tools.py` | The five calculation tools: sales summary, inventory status, supplier terms, restock planner, discount evaluator. |
| `premierpreneur/db.py` | Loads the CSVs into SQLite, drops duplicate sales, flags missing costs. |
| `plan_tool.py` | An `update_plan` tool modelled on OpenAI's Codex harness. |
| `data/` | Synthetic 12-product store with 90 days of sales. `generate_data.py` rebuilds it. |
| `my_agent.py`, `my_tools.py`, `hello.py` | My Milestone 1 agent and setup files. `my_agent.py` is the single-agent starting point. |
| `traces/` | Saved Milestone 1 runs. |
| `run_agent.py`, `premierpreneur/agent.py`, `llm.py`, `trace.py` | An earlier runner from Milestone 1, not used by this version. |

## How to run it (Run each line of code one by one)

Install Ollama, then:

```
pip install -r requirements.txt
ollama pull qwen3:8b
```

Create a file named `Modelfile` containing these two lines (this sets a context size that fits an 8 GB graphics card):

```
FROM qwen3:8b
PARAMETER num_ctx 8192
```

Then:

```
ollama create qwen3-pp -f Modelfile
```

Create a file named `.env` containing `PP_MODEL=qwen3-pp`.

Start the chat page: `python app.py`

It opens at http://127.0.0.1:7860. The database is built from the CSVs the first time it starts.

Run the evaluation: `python eval_harness.py`

Run it with varied wording: `python eval_harness.py --temperature 0.7`

Test the Reviewer on its own: `python check_reviewer.py`

## AI use

**Milestone 1:** Claude helped write the data generator, the five calculation tools and the trace format, and explained each
piece while I built the agent loop, plan tool and retry logic.

**Milestone 2:** Claude wrote most of the Milestone 2 code (`agent_core.py`, `app.py`, `reviewer.py`, `eval_harness.py`, `check_reviewer.py`)
and explained each piece as we built it. I ran every evaluation on my own laptop, found problems in the results and the app,
and decided what to keep and what to rebuild based on those results.