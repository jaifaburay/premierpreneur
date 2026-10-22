import json
import os
from dotenv import load_dotenv
load_dotenv()                      # reads .env so the key never appears on screen
from openai import OpenAI
from premierpreneur.tools import TOOL_SCHEMAS, TOOL_FUNCTIONS   # the five business tools
from plan_tool import update_plan, PLAN_SCHEMA

TOOL_SCHEMAS = TOOL_SCHEMAS + [PLAN_SCHEMA]
TOOL_FUNCTIONS = {**TOOL_FUNCTIONS, "update_plan": update_plan}

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama", timeout=180)
MODEL = "qwen3:8b"

MAX_ITERATIONS = 10          # the stop guard

SYSTEM_PROMPT = """# Role
You are the Business Analyst agent inside PremierPreneur, a decision assistant for a small \
online home-goods retailer. The owner asks about restocking, margins and discounts.

# How you work
- Plan first: on your first turn, state in one or two sentences which tools you will call and why.
- Gather before deciding: sales summary -> inventory status -> supplier terms -> planner / evaluator.
- Before every tool call, write one sentence: what you know so far and why this tool is next.
- Never calculate or estimate a number yourself. Every figure in your answer must appear in a tool result.
- Do not call a tool whose result you already have.

# Tool guidelines
- If a tool returns {"error": ...}, read the message, fix the arguments and retry once. Never retry the \
same call unchanged.
- Report every data gap a tool flags (for example a missing unit cost) and say what the owner must provide.

# Final answer
- A concise recommendation: what to do, the key numbers, assumptions, what is unfunded or unresolved.
- End with: "This is a recommendation for your review; nothing is ordered or changed automatically."
"""


def run(question):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    trace = {"question": question, "model": MODEL,
             "max_iterations": MAX_ITERATIONS, "steps": []}
    attempts = {}        # (tool, args) -> times called; Codex-style bounded retry
    print(f"\nQUESTION: {question}\n")

    for i in range(1, MAX_ITERATIONS + 1):           # <-- the loop IS the guard
        reply = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOL_SCHEMAS, temperature=0,
        )
        msg = reply.choices[0].message
        step = {"iteration": i, "reasoning": msg.content, "tool_calls": []}
        trace["steps"].append(step)
        print(f"--- iteration {i}/{MAX_ITERATIONS} ---")
        if msg.content:
            print("REASONING:", msg.content.strip())

        if not msg.tool_calls:                        # no tool call = the model is done
            print("\nFINAL ANSWER:\n", msg.content)
            trace["final_answer"], trace["stop_reason"] = msg.content, "final_answer"
            json.dump(trace, open("trace.json", "w"), indent=2)
            return msg.content

        # send the model's turn back exactly as it came: Gemini attaches a
        # "thought signature" to each tool call and rejects the next request
        # if it is missing, so rebuilding the message by hand breaks the loop
        messages.append(msg.model_dump(exclude_none=True))

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            print(f"TOOL CALL: {name}({args})")

            key = (name, json.dumps(args, sort_keys=True))
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] > 2:
                result = {"error": f"{name} was already called twice with these arguments; "
                                   f"change the arguments or give your final answer"}
            elif name in TOOL_FUNCTIONS:
                try:
                    result = TOOL_FUNCTIONS[name](**args)
                except TypeError as e:                  # wrong/extra argument names from the model
                    result = {"error": f"bad arguments for {name}: {e}. "
                                       f"Use only the parameters in the tool's schema."}
            else:
                result = {"error": f"unknown tool {name}"}

            step["tool_calls"].append({"tool": name, "arguments": args,
                                       "observation": result})
            print("OBSERVATION:", json.dumps(result)[:200], "...")
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result)})   # observation goes back in

    print(f"\nSTOPPED: max iterations ({MAX_ITERATIONS}) reached without a final answer.")
    trace["final_answer"], trace["stop_reason"] = None, "max_iterations"  
    json.dump(trace, open("trace.json", "w"), indent=2)                   
    return None


if __name__ == "__main__":
    run("I have $1,000 to spend on restocking this week. Which products should I reorder, "
        "how many of each, and what will it cost? Use a moderate strategy. "
        "Tell me what you could not cover.")