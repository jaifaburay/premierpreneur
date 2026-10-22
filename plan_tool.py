"""update_plan - modelled on the Codex harness's plan tool.

The model calls this to write down its plan and tick steps off. It does no
business logic; its value is that the plan becomes visible in the trace.
"""

PLAN = []


def update_plan(steps):
    if not isinstance(steps, list) or not steps:
        return {"error": "steps must be a non-empty list of {step, status}"}
    allowed = {"pending", "in_progress", "completed"}
    for s in steps:
        if not isinstance(s, dict) or "step" not in s or s.get("status") not in allowed:
            return {"error": f"each step needs 'step' text and status in {sorted(allowed)}; got {s}"}
    PLAN.clear()
    PLAN.extend(steps)
    in_progress = sum(1 for s in steps if s["status"] == "in_progress")
    return {"plan": PLAN, "ok": True,
            "note": "keep exactly one step in_progress" if in_progress != 1 else "plan recorded"}


PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": "Record or update your working plan as a list of short steps with status "
                       "pending / in_progress / completed. Call it once at the start, then again as "
                       "steps finish. Keep exactly one step in_progress.",
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        },
                        "required": ["step", "status"],
                    },
                }
            },
            "required": ["steps"],
        },
    },
}