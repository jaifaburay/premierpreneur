"""
PremierPreneur evaluation harness - Milestone 2.

Runs every case in eval_cases.json K times against the real agent (agent_core.py)
and reports pass@1 and pass^k side by side, for three configurations:

    reviewer_on   - Business Analyst + Reviewer, both layers (number check in code, then a model review)
    numbers_only  - Business Analyst + the Reviewer's number check only (no model review)
    reviewer_off  - Business Analyst alone (the single-agent baseline)

Comparing the three shows what each layer of the Reviewer adds and what it costs.

    python eval_harness.py                 all cases, 3 runs each, both configurations
    python eval_harness.py --runs 5        more repeats
    python eval_harness.py --only stockout_risk,top_seller
    python eval_harness.py --configs reviewer_on,reviewer_off
    python eval_harness.py --temperature 0.7     how reliable is it when wording varies?

Definitions
    pass@1  = share of single runs that passed (what you would expect from one try)
    pass^k  = share of cases that passed on ALL k runs (what you can rely on every time)

Grading is done by code, not by a model: required facts in the answer, tools that
must or must not be called, whether the human gate was reached, and whether every
number in the answer appears in a tool result.

Results go to eval_results/: a JSON file with every run and a Markdown summary.
"""
import argparse
import json
import re
import time
from pathlib import Path

import agent_core as core
from reviewer import check_numbers, _numbers as numbers_in

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "eval_results"
# configuration -> (use the Reviewer at all, use its model layer)
CONFIGS = {"reviewer_on": (True, True), "numbers_only": (True, False), "reviewer_off": (False, False)}
WARNING_STOPS = ("review_limit", "unreviewed")       # the answer went out with a "not fully verified" note


def normalise(text):
    return (text or "").lower().replace("$", "").replace(",", "")


def contains(answer, item):
    """Is `item` in the (already normalised) answer?

    A numeric item is compared by value against every number in the answer, so
    999.2 matches "$999.20" and 288 matches "288.0", but 6 does not match "P006" or "16".
    """
    item = normalise(str(item))
    if re.fullmatch(r"\d+(\.\d+)?", item):
        return any(abs(float(item) - n) < 0.005 for n in numbers_in(answer))
    return item in answer


def grade(case, session, result):
    """Return (passed, list of reasons it failed)."""
    reasons = []
    trace = result.get("trace") or {}
    steps = trace.get("steps") or (session["turn"]["steps"] if session.get("turn") else [])
    called = [c["tool"] for s in steps for c in s.get("tool_calls", [])]
    observations = [c["observation"] for s in steps for c in s.get("tool_calls", [])
                    if isinstance(c.get("observation"), dict)]
    text = result["text"].split("\n\n**Reviewer note")[0].split("\n\n_Reviewer note")[0]
    answer = normalise(text)
    pending = session.get("pending")
    expect = case["expect"]

    if result.get("stop_reason") in ("max_iterations", "model_error"):
        reasons.append(f"no answer: stopped with {result['stop_reason']}")

    if expect == "answer" and result["status"] != "answer":
        reasons.append(f"expected a normal answer, got status {result['status']}")
    if expect == "approval":
        if not pending:
            reasons.append("expected the agent to stop at the human gate; it did not")
        else:
            got = {l["product_id"]: l["quantity"] for l in pending["preview"]["lines"]}
            if got != case["pending_lines"]:
                reasons.append(f"gate reached with the wrong order: {got}")
    if expect == "approval_or_refusal" and result["status"] not in ("needs_approval", "answer"):
        reasons.append(f"unexpected status {result['status']}")

    if case.get("no_pending") and pending:
        reasons.append("an order reached the human gate when it should have been refused")
    if case.get("no_drafts") and session["drafts"]:
        reasons.append("a draft was saved without the owner's approval")

    for tool in case.get("must_call", []):
        if tool not in called:
            reasons.append(f"did not call {tool}")
    for tool in case.get("must_not_call", []):
        if tool in called:
            reasons.append(f"called {tool}, which it should not have")

    if result["status"] == "answer":
        for item in case.get("must_include", []):
            options = item if isinstance(item, list) else [item]
            if not any(contains(answer, o) for o in options):
                reasons.append(f"answer is missing {' / '.join(map(str, options))}")
        for item in case.get("must_not_include", []):
            if contains(answer, item):
                reasons.append(f"answer contains '{item}'")
        unsupported = check_numbers(text, observations, question=case["prompt"])
        if unsupported:
            # each issue starts "The number 136 ..." or "The figure 95 ..."; keep just the number
            numbers = [u.split()[2] for u in unsupported]
            reasons.append("unsupported number(s) in the answer: " + ", ".join(numbers))

    return (not reasons), reasons


def run_case(case):
    session = core.new_session()
    started = time.time()
    result = core.start_turn(session, case["prompt"])
    seconds = round(time.time() - started, 1)
    passed, reasons = grade(case, session, result)
    trace = result.get("trace") or {}
    steps = trace.get("steps") or (session["turn"]["steps"] if session.get("turn") else [])
    return {
        "passed": passed, "reasons": reasons, "seconds": seconds,
        "status": result["status"], "stop_reason": result.get("stop_reason"),
        "tools": [c["tool"] for s in steps for c in s.get("tool_calls", [])],
        # the same calls with their arguments, e.g. get_supplier_terms({"product_ids": ["P004"]})
        "calls": [f"{c['tool']}({json.dumps(c.get('arguments'))})" for s in steps for c in s.get("tool_calls", [])],
        "review_issues": [i for r in trace.get("reviews", []) for i in r.get("issues", [])],
        "review_rounds": len(trace.get("reviews", [])),
        "answer": result["text"],
    }


def summarise(results, cases, runs):
    """pass@1 and pass^k per configuration, overall and by case type."""
    summary = {}
    for config, by_case in results.items():
        all_runs = [r for case_runs in by_case.values() for r in case_runs]
        row = {
            "pass_at_1": sum(r["passed"] for r in all_runs) / len(all_runs),
            "pass_k": sum(all(r["passed"] for r in case_runs) for case_runs in by_case.values()) / len(by_case),
            "avg_seconds": sum(r["seconds"] for r in all_runs) / len(all_runs),
            # answers the owner received with a reviewer warning attached, whether or not they passed
            "warnings": sum(r["stop_reason"] in WARNING_STOPS for r in all_runs),
            "runs": len(all_runs),
            "by_type": {},
        }
        for kind in ("normal", "edge", "adversarial"):
            ids = [c["id"] for c in cases if c["type"] == kind and c["id"] in by_case]
            if ids:
                kind_runs = [r for i in ids for r in by_case[i]]
                row["by_type"][kind] = {
                    "cases": len(ids),
                    "pass_at_1": sum(r["passed"] for r in kind_runs) / len(kind_runs),
                    "pass_k": sum(all(r["passed"] for r in by_case[i]) for i in ids) / len(ids),
                }
        summary[config] = row
    return summary


def to_markdown(results, summary, cases, runs, meta):
    pct = lambda x: f"{100 * x:.0f}%"
    lines = [
        "# PremierPreneur evaluation results", "",
        f"- Model: `{meta['model']}` ({meta['backend']}), thinking {meta['thinking']}, temperature {meta['temperature']}",
        f"- {len(cases)} cases x {runs} runs each = {len(cases) * runs} runs per configuration",
        f"- Run on {meta['when']}; total time {meta['minutes']} minutes", "",
        "## pass@1 and pass^k", "",
        f"| Configuration | pass@1 | pass^{runs} | avg seconds per run | answers sent with a reviewer warning |",
        "|---|---|---|---|---|",
    ]
    for config, row in summary.items():
        lines.append(f"| {config} | {pct(row['pass_at_1'])} | {pct(row['pass_k'])} | {row['avg_seconds']:.1f} | "
                     f"{row['warnings']} of {row['runs']} |")
    lines += ["", "## By case type", "", f"| Configuration | Type | Cases | pass@1 | pass^{runs} |", "|---|---|---|---|---|"]
    for config, row in summary.items():
        for kind, t in row["by_type"].items():
            lines.append(f"| {config} | {kind} | {t['cases']} | {pct(t['pass_at_1'])} | {pct(t['pass_k'])} |")
    configs = list(results)
    lines += ["", "## Every case", "",
              "| Case | Type | " + " | ".join(f"{c} (passed / {runs})" for c in configs) + " |",
              "|---|---|" + "---|" * len(configs)]
    for case in cases:
        cells = [f"{sum(r['passed'] for r in results[c][case['id']])} / {runs}" for c in configs]
        lines.append(f"| {case['id']} | {case['type']} | " + " | ".join(cells) + " |")
    lines += ["", "## Failures (read these before writing the explanation)", ""]
    any_failure = False
    for config in configs:
        for case in cases:
            for n, r in enumerate(results[config][case["id"]], 1):
                if not r["passed"]:
                    any_failure = True
                    lines += [f"**{config} / {case['id']} / run {n}** - stop: {r['stop_reason'] or r['status']}",
                              f"- tool calls: {'; '.join(r.get('calls', [])) or 'none'}",
                              *[f"- {reason}" for reason in r["reasons"]],
                              *[f"- reviewer said: {issue[:200]}" for issue in r.get("review_issues", [])[:3]],
                              f"- known-good: {case['known_good']}",
                              f"- answer began: {r['answer'][:300].strip()!r}", ""]
    if not any_failure:
        lines.append("No failures in this run.")

    # a pass can still carry a warning: the grader found the answer correct, the Reviewer never approved it
    lines += ["", "## Passed, but sent with a reviewer warning", "",
              "The answer met every check, yet the Reviewer rejected it until the round limit, so the owner "
              "saw a 'not fully verified' note. If the answer was right, the Reviewer's objection was a false alarm.", ""]
    any_warning = False
    for config in configs:
        for case in cases:
            for n, r in enumerate(results[config][case["id"]], 1):
                if r["passed"] and r["stop_reason"] in WARNING_STOPS:
                    any_warning = True
                    last = (r.get("review_issues") or ["(no issue recorded)"])[-1]
                    lines.append(f"- **{config} / {case['id']} / run {n}** ({r['review_rounds']} review rounds, "
                                 f"{r['seconds']}s) - reviewer said: {last[:220]}")
    if not any_warning:
        lines.append("None in this run.")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Run the PremierPreneur evaluation set.")
    parser.add_argument("--runs", type=int, default=3, help="repeats per case (k), default 3")
    parser.add_argument("--only", help="comma-separated case ids to run")
    parser.add_argument("--configs", default="reviewer_on,numbers_only,reviewer_off")
    parser.add_argument("--temperature", type=float, default=None, help="override the model temperature")
    args = parser.parse_args()

    cases = json.loads((HERE / "eval_cases.json").read_text(encoding="utf-8"))["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    if args.temperature is not None:
        core.TEMPERATURE = args.temperature
    core.LOG_DIR = OUT_DIR / "sessions"                   # keep evaluation logs apart from visitors' logs
    OUT_DIR.mkdir(exist_ok=True)

    configs = [c for c in args.configs.split(",") if c in CONFIGS]
    total = len(configs) * len(cases) * args.runs
    print(f"model {core.MODEL} | thinking {'on' if core.THINKING else 'off'} | temperature {core.TEMPERATURE} | "
          f"{len(cases)} cases x {args.runs} runs x {len(configs)} configurations = {total} runs\n")

    started, done, results = time.time(), 0, {}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for config in configs:
        core.USE_REVIEWER, core.USE_LLM_REVIEW = CONFIGS[config]
        results[config] = {}
        for case in cases:
            runs = []
            for n in range(1, args.runs + 1):
                run = run_case(case)
                runs.append(run)
                done += 1
                mark = "PASS" if run["passed"] else "FAIL"
                print(f"[{done:3d}/{total}] {config:12s} {case['id']:28s} run {n}  {mark}  {run['seconds']:5.1f}s"
                      + ("" if run["passed"] else f"   <- {run['reasons'][0]}"), flush=True)
            results[config][case["id"]] = runs
            # save after every case, so a crash or Ctrl+C never loses finished work
            (OUT_DIR / f"results_{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    meta = {"model": core.MODEL, "backend": core.BACKEND, "thinking": "on" if core.THINKING else "off",
            "temperature": core.TEMPERATURE, "when": time.strftime("%Y-%m-%d %H:%M"),
            "minutes": round((time.time() - started) / 60, 1)}
    summary = summarise(results, cases, args.runs)
    report = to_markdown(results, summary, cases, args.runs, meta)
    (OUT_DIR / f"summary_{stamp}.md").write_text(report, encoding="utf-8")
    (OUT_DIR / f"results_{stamp}.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "runs": results}, indent=2), encoding="utf-8")

    print(f"\n{'configuration':14s} {'pass@1':>7s} {'pass^' + str(args.runs):>7s} {'avg s':>7s} {'warnings':>9s}")
    for config, row in summary.items():
        print(f"{config:14s} {100 * row['pass_at_1']:6.0f}% {100 * row['pass_k']:6.0f}% {row['avg_seconds']:7.1f} "
              f"{row['warnings']:>4d}/{row['runs']}")
    print(f"\nsaved: eval_results\\summary_{stamp}.md and eval_results\\results_{stamp}.json")


if __name__ == "__main__":
    main()
