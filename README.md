# PremierPreneur

This is a business decision assistant for small retailers, built for my CIS 4394 (Agentic AI) at Georgia State University's Robinson College of Business.
An AI agent answers an owner's question like *"I have $1,000 to restock this week, what should I order?"*
by calling calculation tools over the store's sales and inventory data. The model never does the math;
the tools do, and the model explains the result.

**Milestone 3 (this version):** Added Excel and CSV upload with a cleaning step, tools that work on other datasets and on bigger ones, two more agents (a Planner and a Decision agent), and a security test suite of 25 attacks.

**Milestone 2:** Created a chat page with a public link, a second agent that reviews every answer, a human approval step before the one action the agent can take, and an evaluation harness that reports pass@1 and pass^k.

## Read this first: what this version is and is not
- It's only been tested on a made-up store of synthetic data found in data/. That store has 12 products and 90 days of sales, and I built the tools and the test cases around it. The results below say nothing about how it would do on a real business.
- New in Milestone 3: it can load someone else's Excel or CSV file, but it has only been tried by hand on one real public file (Online Retail II from Kaggle, about 1 million rows), with three questions. Any table can be cleaned, loaded and asked about. Restock advice also needs stock levels, unit costs and a supplier lead time. When a file lacks them, the assistant says what is missing.
- Only part of Milestone 3 has been measured on the real model. I ran the security suite on Qwen on Nov 19: 25 attacks, 3 runs each, and 23 were stopped in all 3 runs. The 14 original cases, the second dataset and the Decision cases haven't been re-run on this version, so the results table below is still the Milestone 2 one.
- It's only been evaluated on one model: Qwen3-8B (4-bit) running locally through Ollama, with thinking turned off. The code can also call Gemini, but I did not evaluate that in this milestone.
- It's a class project and is not ready to be used properly by anyone. Use it at your own discretion. Do not use its answers for any real purchasing decisions.


## How it works
1. The owner types a question on the chat page. New in Milestone 3: to use their own data, they first upload an Excel or CSV file. Plain code cleans it (no model), the owner checks the cleaning report, and nothing is used until they press "Use this data".
2. New in Milestone 3: the Planner agent reads the question and a summary of the data, and says which tools to use or what data is missing. It sees no data values and has no tools.
3. The Business Analyst agent either calls a tool or drafts an answer. Tools run in Python over a SQLite database, and the results go back to the model.
4. New in Milestone 3: when the Analyst compares the three restock scenarios, the Decision agent recommends one for the owner's goal. Code does every comparison between the scenarios.
5. The Reviewer agent checks the draft before the owner sees it, using 4 main things:
   - a number check in code: every number must come from a tool result
   - a product check in code: the tools must have looked at the product the owner asked about
   - new in Milestone 3, a completeness check in code: a restock answer must state the total and name the products left out for missing data
   - two yes/no questions answered by the model in a clean context: is the draft about what was asked, and does it claim something was ordered, paid or sent?
6. If the draft fails, it goes back to the Analyst with the exact problem, up to 2 times. After that, the answer is shown with a note that it was not fully verified.
7. If the Analyst wants to save an order draft, the loop stops and shows an approval card with the exact arguments and catalog prices. Nothing is saved unless the owner answers Yes. An approved draft can be downloaded as an Excel file. No supplier is contacted and nothing is bought.
8. New in Milestone 3: links, images and HTML are stripped from tool results before a model reads them, and from answers before the owner sees them.
9. Every step is saved to a session log on the laptop running the app.

## Evaluation results

The table and findings below are from Milestone 2. What I have measured for Milestone 3 so far is at the end of this section.

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

Milestone 3 so far:

- The offline tests in `tests/` (1,283 checks in quick mode, using a scripted stand-in for the model) pass on my Windows laptop. They show the code works, not how well the model uses it.
- Security suite on Qwen at temperature 0: 25 attacks, each run 3 times. 23 were stopped in all 3 runs.
- The model printed its own rules when asked (failed 3 of 3). I added a code check that withholds an answer that repeats the rules, and it then passed 3 of 3.
- **Still open:** two attacks fail in every run (`sec_planted_number` and `sec_confirm_refuted_number`). Both are about a false figure from an earlier turn.
- One real public file, tried by hand: Online Retail II (1,067,371 rows). The cleaning step kept 1,033,036 rows after dropping exact duplicates, and the two sales answers (top seller, last 30 days against the 30 before) matched a separate count of the file.
- On that same file the agent failed a restock question. The file has no stock levels or costs, but it recommended a product anyway and quoted a price from an unrelated row, and the Reviewer approved it. A code check now refuses a question that needs data the file doesn't have, before any model runs. That check passes its offline tests but has not been re-run on the real model.
- **Still open:** cancelled orders are left out instead of subtracted, so on that file an order of 80,995 units that was cancelled the same day counts as the best seller.
- The "still open" item from Milestone 2 now has a completeness check, but it has not been measured on the real model.
- Not yet re-run on this version: the 14 cases above, the second dataset (`eval_cases_retail.json`), the Decision cases and the three `check_*.py` scripts.

## Dev notes: where this stands against the proposal

| From the proposal | Status in Milestone 2 | Added in Milestone 3 | What is left |
|---|---|---|---|
| Business Analyst agent | Built. One loop, five calculation tools, memory of the last 3 turns, stop guard and caps. | Nine tools instead of five, including a general query tool, and a context budget. | Results on the real model with the new tools. |
| Reviewer agent | Built and evaluated against the single-agent baseline. | A check for missing content in restock answers. Numbers are only trusted if they came from a tool result. | Results on the real model. A test with thinking turned on. |
| Planner agent | Not a separate agent. The Analyst has an `update_plan` tool and is told to plan first. | Built as a separate agent. Its plan decides which tools the Analyst is shown. | Test it against the baseline on the real model (`check_planner.py` and the "no Planner" run). |
| Decision Agent and the three decision temperatures | Partly. The restock planner accepts conservative, moderate or aggressive, but one at a time. | Built as a separate agent, on top of a tool that compares the three side by side. Code does every comparison. | Test it on the real model (`check_decision.py`). |
| Owner reviews everything, the agent never places orders | Built. Approval card, Excel draft, no tool that orders or pays. | No change. | Nothing for this milestone. |
| CSV uploads into SQLite | Not built. The CSVs in `data/` are loaded by a script. | Built: upload for CSV and Excel, cleaning, "invalid" for blank or unreadable files, removal of instructions and links hidden in cells. | Testing on real exported files. Very large Excel files are slow (CSV is fast). |
| Dashboard with metrics, inventory views and saved recommendations | Not built. This version is a chat page. | Not built. The chat page now has an upload section. | The whole dashboard. |
| Real-world use cases | Not started. Synthetic data only. | Partly. A generated retail file in the layout of public datasets, with its own 14 test cases. One real public file (Online Retail II) tried by hand. | Test cases with known-good answers for a real public dataset. |

Added since the proposal and also not built: creating a starter dataset from an owner's spoken description when they have no records, and attaching OpenAI's Codex harness directly to Qwen (this version borrows the Codex design: a sectioned system prompt, a plan tool and bounded retries).

Other known limits:

- The product check can wrongly reject an answer if the owner misspells a product name.
- 14 test cases is a small set, and the known-good answers belong to this one store.
- Thinking is off for speed (about 15 seconds for a restock answer instead of about 190), which weakens the model's judgment.
- The public link runs on my laptop and only works while the app is running.

New limits in Milestone 3:

- On the real model, only the security suite has been run on this version.
- Session logs keep tool results, including figures and names from uploaded files, until I delete them by hand. Uploaded files themselves are deleted after 24 hours unused.
- Removing instruction-like text from uploaded files is pattern based. It misses other languages and reworded instructions, so the other guards matter (no tool that sends anything out, the approval card, stripped links).
- Dates like 03/04/2024 are read month first, with a warning in the cleaning report.

## Files

| File | What it is |
|---|---|
| `app.py` | The chat page (Gradio), now with file upload and the cleaning report. Run this. |
| `agent_core.py` | The Business Analyst loop, the approval gate, memory, caps and session logging. Milestone 3 adds the calls to the Planner and Decision agents and a context budget. |
| `planner.py` | New in Milestone 3. The Planner agent. |
| `decision.py` | New in Milestone 3. The Decision agent. |
| `reviewer.py` | The Reviewer: number check, product check, completeness check and the model's two questions. |
| `guards.py` | New in Milestone 3. Plain-code guards: strips links, images and HTML, and checks a restock answer is complete. |
| `ingest/` | New in Milestone 3. Reads, cleans and loads an Excel or CSV file into SQLite, with no model. Says "invalid" and why for files it can't use. |
| `check_reviewer.py` | Tests the Reviewer on its own with drafts whose correct verdict is known. |
| `check_planner.py`, `check_decision.py` | New in Milestone 3. The same kind of test for the Planner and the Decision agent. |
| `eval_harness.py` | Runs every test case several times and reports pass@1 and pass^k. |
| `eval_cases.json` | The 14 test cases and their known-good answers. |
| `eval_cases_security.json` | New in Milestone 3. 25 attack cases, each labelled with the input it uses and the guard that should stop it. |
| `eval_cases_retail.json` | New in Milestone 3. 14 cases on a generated, messy retail file. |
| `eval_cases_decision.json` | New in Milestone 3. 4 cases for the Decision agent. |
| `eval_cases_offline.json` | New in Milestone 3. Scripts for running the harness without a model. |
| `tests/` | New in Milestone 3. Offline tests with a scripted stand-in for the model. |
| `eval_results/` | Results of every evaluation run. |
| `check_m1_answer.py` | Runs the number check on a saved Milestone 1 trace. |
| `premierpreneur/tools.py` | The calculation tools: sales summary, inventory status, supplier terms, restock planner, discount evaluator. Milestone 3 adds four: data description, general query, scenario comparison and period comparison. |
| `premierpreneur/db.py` | Loads the CSVs into SQLite, drops duplicate sales, flags missing costs. |
| `plan_tool.py` | An `update_plan` tool modelled on OpenAI's Codex harness. Now only used when the Planner agent is switched off. |
| `data/` | Synthetic 12-product store with 90 days of sales. `generate_data.py` rebuilds it. `make_retail_dataset.py` (new in Milestone 3) generates the second test dataset. |
| `docs/` | `M3_TEST_GUIDE.md` lists the Milestone 3 runs to do on the laptop. `M3_BUILD_SPEC.md` is the design the Milestone 3 parts were built against. |
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

New in Milestone 3:

Run the offline tests (no model needed): `python tests/run_all.py`

Run the security suite: `python eval_harness.py --cases eval_cases_security.json`

Run the second dataset: `python eval_harness.py --cases eval_cases_retail.json`

Test the Planner and the Decision agent on their own: `python check_planner.py` and `python check_decision.py`

Switches (set in `.env`): `PP_PLANNER=0`, `PP_DECISION=0` and `PP_REVIEWER=0` turn an agent off. `PP_UPLOADS=0` hides file upload.

## AI use

**Milestone 1:** Claude helped write the data generator, the five calculation tools and the trace format, and explained each
piece while I built the agent loop, plan tool and retry logic.

**Milestone 2:** Claude wrote most of the Milestone 2 code (`agent_core.py`, `app.py`, `reviewer.py`, `eval_harness.py`, `check_reviewer.py`)
and explained each piece as we built it. I ran every evaluation on my own laptop, found problems in the results and the app,
and decided what to keep and what to rebuild based on those results.

**Milestone 3:** Claude wrote most of the Milestone 3 code, using separate helper agents for the file cleaning (`ingest/`), the tools, the Planner and Decision agents, the upload page and the evaluation harness, and it wrote the offline tests. I set the scope, ran the offline tests and the security suite on my own laptop, and reported what failed (the prompt leak and the two planted-number cases).
