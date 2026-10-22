#!/usr/bin/env python3
"""
PremierPreneur - Milestone 1 runner.

Examples
--------
# offline demo (no API key needed)
python run_agent.py --backend mock --scenario restock

# real run on OpenAI (export OPENAI_API_KEY first)
python run_agent.py --backend openai --model gpt-4o-mini --scenario restock

# local model through Ollama
python run_agent.py --backend ollama --model llama3.1:8b --scenario restock

# your own question
python run_agent.py --backend openai -q "I have $1,500 to restock. Be conservative. What should I order?"

# show the max-iteration guard firing
python run_agent.py --backend mock --scenario restock --max-iterations 3

# rebuild the synthetic data + SQLite database from scratch
python run_agent.py --rebuild-data
"""
from __future__ import annotations

import argparse
import sys

SCENARIOS = {
    "restock": (
        "I have $1,000 to spend on restocking this week. Which products should I reorder, how many of each, "
        "and what will it cost? Use a moderate strategy. Tell me what you could not cover."
    ),
    "discount": (
        "The Herb Garden Starter Kit (P010) is not moving. If I put it on a 30% discount, what happens to my "
        "margin and how many would I need to sell to keep my gross profit the same?"
    ),
    "restock-conservative": (
        "Budget is tight: $600 for restocking. Be conservative and only protect products that are about to "
        "run out before the next delivery could arrive."
    ),
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Run the PremierPreneur Business Analyst agent on one question.")
    p.add_argument("-q", "--question", help="free-form owner question")
    p.add_argument("--scenario", choices=sorted(SCENARIOS), help="use a built-in question")
    p.add_argument("--backend", default="mock", choices=["openai", "ollama", "mock"])
    p.add_argument("--model", help="model name (default: gpt-4o-mini for openai, llama3.1:8b for ollama)")
    p.add_argument("--max-iterations", type=int, default=8, help="stop guard (default 8)")
    p.add_argument("--quiet", action="store_true", help="only print the final answer and trace paths")
    p.add_argument("--rebuild-data", action="store_true", help="regenerate CSVs and rebuild the SQLite db, then exit")
    args = p.parse_args(argv)

    if args.rebuild_data:
        from data.generate_data import generate  # noqa: WPS433
        from premierpreneur.db import build_database, DB_PATH
        counts = generate()
        if DB_PATH.exists():
            DB_PATH.unlink()
        report = build_database()
        print("generated:", counts)
        for t, info in report.items():
            print(f"  {t:10s} loaded={info['loaded']:5d} rejected={info['rejected']:3d} {info['notes']}")
        return 0

    question = args.question or SCENARIOS.get(args.scenario or "", None)
    if not question:
        p.error("give --question or --scenario (restock | discount | restock-conservative)")

    from premierpreneur.agent import run_question

    trace = run_question(question, backend=args.backend, model=args.model,
                         max_iterations=args.max_iterations, verbose=not args.quiet)
    if args.quiet:
        print(trace.final_answer or "")
        print(f"\n[stop_reason={trace.stop_reason} iterations={trace.iterations_used} "
              f"tool_calls={sum(len(s.tool_calls) for s in trace.steps)}]")
    return 0 if trace.stop_reason == "final_answer" else 2


if __name__ == "__main__":
    sys.exit(main())
