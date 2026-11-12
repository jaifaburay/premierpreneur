"""Step 1 demo: run the Reviewer's number check on the Milestone 1 answer.

    python check_m1_answer.py traces\trace_restock_qwen.json
"""
import json
import sys

from reviewer import check_numbers

trace = json.load(open(sys.argv[1], encoding="utf-8"))
observations = [call["observation"] for step in trace["steps"] for call in step["tool_calls"]]
issues = check_numbers(trace["final_answer"], observations, question=trace["question"])

print(f"question: {trace['question'][:70]}...")
print(f"tool results checked against: {len(observations)}")
if issues:
    print(f"REJECTED - {len(issues)} unsupported number(s):")
    for issue in issues:
        print("  -", issue)
else:
    print("PASSED - every number in the answer appears in a tool result")
