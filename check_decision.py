"""
Test the Decision agent on its own.

The evaluation harness tests the whole system. This file tests only the Decision
agent (choose_scenario() in decision.py): it hands the agent owner goals whose
right scenario is already known and counts how often it picks it. It needs the
real model.

    python check_decision.py            every goal 3 times
    python check_decision.py --runs 5

The comparison comes from the real compare_scenarios tool on the built-in store
with a $5,000 budget, where the three scenarios differ:
    conservative   the cheapest, and the one that gives the most products an order
    moderate       the middle plan
    aggressive     the most units, and the longest cover aimed for
One more case uses a $1,000 budget, where all three scenarios place the same
order: code must notice that and recommend none, without calling the model.

Each goal is labelled with the scenario that fits it, or with two where both can
be defended. Three results are counted:
    correct     the recommendation is one of the accepted scenarios
    wrong       a recommendation that passed every code check but does not fit
                the goal. This is the dangerous kind: the owner would see it.
    unreadable  no recommendation: the reply was malformed, or a code check
                threw it away (a choice that contradicts the stated priority, a
                number that is not in the comparison). Harmless, but if it
                happens often the agent is not useful.
"""
import argparse
import time

import agent_core as core
from decision import choose_scenario
from premierpreneur import tools

BUDGET = 5000           # the three scenarios differ at this budget
SAME_ORDER_BUDGET = 1000    # ... and place the same order at this one

CASES = [
    {"id": "cash_tight", "accept": {"conservative"},
     "goal": "Cash is tight this month. I want to spend as little as I can."},
    {"id": "keep_cash", "accept": {"conservative"},
     "goal": "I want to keep as much money in the bank as possible."},
    {"id": "holiday_rush", "accept": {"aggressive"},
     "goal": "The holiday rush is coming. Buy as many units as the budget allows."},
    {"id": "long_cover", "accept": {"aggressive"},
     "goal": "I do not want to reorder again for a long time, so stock up for as many days as possible."},
    {"id": "no_goal", "accept": {"moderate"},
     "goal": ""},
    {"id": "middle_road", "accept": {"moderate"},
     "goal": "Nothing special is going on. Give me a sensible middle-of-the-road plan."},
    # two priorities at once: the cheap plan and the middle plan can both be defended, the aggressive one cannot
    {"id": "cash_and_stock", "accept": {"conservative", "moderate"},
     "goal": "Keep some cash back for me, but do not leave me short on stock either."},
    # the budget must be used and the owner wants stock: moderate and aggressive both spend nearly all of it
    {"id": "use_the_budget", "accept": {"moderate", "aggressive"},
     "goal": "This budget is use-it-or-lose-it, so put all of it to work and stock up."},
    # the tool's stock-out risk figure is the same for all three plans here, so no plan is wrong on that
    # ground; this case measures whether the agent stays readable when its priority does not separate them
    {"id": "avoid_stockouts", "accept": {"conservative", "moderate", "aggressive"},
     "goal": "My biggest worry is running out of stock. Avoid stock-outs above all."},
    {"id": "same_order", "accept": {None}, "budget": SAME_ORDER_BUDGET,
     "goal": "Cash is tight this month. I want to spend as little as I can."},
]


def grade(case, result):
    """Compare one result with its label. Returns "correct", "unreadable" or "wrong"."""
    if result["status"] != "ok":
        return "unreadable"
    return "correct" if result["recommended"] in case["accept"] else "wrong"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    runs = parser.parse_args().runs
    core.ensure_database()
    client = core.get_client()
    comparisons = {budget: tools.compare_scenarios(budget) for budget in (BUDGET, SAME_ORDER_BUDGET)}
    print(f"model {core.MODEL} | {len(CASES)} goals x {runs} runs | budget ${BUDGET:,} "
          f"(one case at ${SAME_ORDER_BUDGET:,})\n")

    wrong = unreadable = correct = 0
    for case in CASES:
        marks, last, started = [], None, time.time()
        for _ in range(runs):
            result = choose_scenario(client, core.MODEL, comparisons[case.get("budget", BUDGET)], case["goal"],
                                     model_options=core.MODEL_OPTIONS)
            mark = grade(case, result)
            if mark == "correct":
                correct += 1
                marks.append(f"ok ({result['recommended'] or 'none: decided in code'})")
            elif mark == "unreadable":
                unreadable += 1
                marks.append("unreadable")
                last = result
            else:
                wrong += 1
                marks.append(f"WRONG ({result['recommended']})")
                last = result
        seconds = (time.time() - started) / runs
        accept = " or ".join(sorted(str(name) for name in case["accept"]))
        if len(case["accept"]) == 3:
            accept = "any of the three"
        print(f"{case['id']:16s} accept {accept:26s} {', '.join(marks):52s} {seconds:4.1f}s each")
        if last is not None:
            print(f"    last miss: read the goal as {last['priority']} | {last['reason'][:170]}")

    total = len(CASES) * runs
    print(f"\nwrong choices: {wrong} of {total}"
          f"\nunreadable:    {unreadable} of {total}"
          f"\ncorrect:       {correct} of {total}")


if __name__ == "__main__":
    main()
