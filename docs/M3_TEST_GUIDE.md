# Milestone 3: what to run on the laptop, in order

Everything in this build was tested with a scripted stand-in for the model, on Linux.
Nothing has been run against Qwen or on Windows yet. These steps do that. Run them in
Command Prompt from the project folder, one at a time, and stop at the first step that
fails: later steps depend on earlier ones.

## 0. Set up (5 minutes)

1. Unzip the build into a NEW folder, next to your current project folder. Do not unzip over
   your GitHub Desktop folder yet.
2. Copy your `.env` and `Modelfile` from the old folder into the new one.
3. Install the two new libraries:

   ```
   pip install -r requirements.txt
   ```

4. Make sure Ollama is running and the model is there: `ollama list` should show `qwen3-pp`.

## 1. Offline tests: does the code work on Windows? (about 2 minutes, no model)

```
python tests\run_all.py
```

Expected: every line ends in PASS and the last line says 0 failed (about 1,300 checks).
This proves the cleaning, the tools, the guards, the approval gate and the harness on your
machine. If anything fails, send me the lines that say FAIL.

## 2. Each agent on its own, with the real model (about 1 minute each)

```
python check_reviewer.py
python check_planner.py
python check_decision.py
```

Each prints how many answers were right, wrong or unreadable. Send me all three outputs.
What good looks like: the Reviewer has 0 false alarms and 0 misses; the Planner and the
Decision agent have few wrong answers and 0 unreadable ones.

## 3. The 14 original cases, with every agent on and off (about 15 minutes)

```
python eval_harness.py
```

This runs three configurations: `full` (all four agents), `no_planner` (the Milestone 2
system plus the Decision agent) and `analyst_alone` (the baseline). Milestone 2 scored
100% / 100% for the full system and 93% / 93% for the Analyst alone at temperature 0.
The system prompt and the tool list changed, so the score may have changed too.
Send me the `summary_...md` file from `eval_results`.

## 4. The Decision agent in a real turn (about 3 minutes)

```
python eval_harness.py --cases eval_cases_decision.json
```

## 5. A second, messy dataset (about 10 minutes)

```
python eval_harness.py --cases eval_cases_retail.json
```

The harness generates a 20,000-row retail file with realistic dirt, cleans it, and runs 14
cases whose correct answers were counted independently of the tools. This is the test of
"works on data it was not built for".

## 6. The security suite (about 20 minutes)

```
python eval_harness.py --cases eval_cases_security.json
```

25 attacks, 3 runs each. The summary ends with a table: attack, which input it arrives
through, which leg of the lethal trifecta it tests, which guard should stop it, and how many
runs passed. That table is the evidence for the security review.

## 7. Varied wording (about 15 minutes)

```
python eval_harness.py --temperature 0.7
```

## 8. The app, by hand

```
python app.py
```

1. Ask one of the example questions on the sample store.
2. Open "Your data" and upload a spreadsheet of your own, the messier the better.
   Read the cleaning report: is anything wrong or surprising?
3. Press "Use this data" and ask three questions about it.
4. Upload a blank workbook and a text file renamed to `.xlsx`. Both should be refused with a reason.
5. Download one public retail dataset (for example from Kaggle) and upload it. This is the
   one test I could not do: I cannot download files from Kaggle.

## 9. A big file

```
python data\make_retail_dataset.py --rows 1000000 --out big.csv
python -m ingest big.csv --db big.db
```

Then upload `big.csv` in the app and ask a question. Delete `big.csv` and `big.db` afterwards.

## What to send back

The output of steps 1 and 2, and the newest `summary_...md` from steps 3 to 7. With those I
can fix what the real model gets wrong and give you the facts for the security review.
