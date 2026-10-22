"""
The agent loop.

Milestone 1 runs ONE agent - the Business Analyst - end to end:

    for i in 1..max_iterations:
        model reasons and either calls a tool or answers
        tool runs (deterministic Python over SQLite)
        observation goes back to the model
    stop when the model answers without a tool call      -> stop_reason = final_answer
    or when the iteration budget is exhausted             -> stop_reason = max_iterations

Every iteration is written to a Trace. The Planner / Decision / Reviewer
agents from the proposal are Milestone 2; this file is written so they can
wrap this loop rather than replace it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import tools as toolbox
from .db import ensure_database
from .llm import ChatResult, make_backend
from .trace import Trace

AGENT_NAME = "Business Analyst"
DEFAULT_MAX_ITERATIONS = 8

SYSTEM_PROMPT = """You are the Business Analyst agent inside PremierPreneur, a decision assistant for a small \
online home-goods retailer. The store owner asks business questions about restocking, margins and discounts.

Rules you must follow:
1. NEVER calculate or estimate a number yourself. Every figure in your answer must come from a tool result. \
If a number is not available from a tool, say so.
2. Before each tool call, write one or two sentences of reasoning: what you know so far and why this tool is the \
next step. Keep it short.
3. Gather context before planning: typically sales summary -> inventory status -> supplier terms -> the planning \
or evaluation tool. Do not call a tool whose result you already have.
4. If a tool returns an error, read the message, fix the arguments and retry once. Do not loop.
5. Report every data gap the tools flag (for example a product with no unit cost) and say what the owner must \
provide to resolve it.
6. Your final answer must be a concise recommendation for the owner: what to do, the key numbers, what was \
assumed, what is unfunded or unresolved. End by noting that it is a recommendation for their review; nothing is \
ordered or changed automatically.
7. When you have enough evidence, answer directly without calling any more tools.
"""


class BusinessAnalystAgent:
    def __init__(self, backend, max_iterations: int = DEFAULT_MAX_ITERATIONS, db_path: Path | None = None,
                 verbose: bool = True):
        self.backend = backend
        self.max_iterations = max_iterations
        self.db_path = db_path
        self.verbose = verbose
        ensure_database(db_path) if db_path else ensure_database()

    # ------------------------------------------------------------------ run
    def run(self, question: str) -> Trace:
        trace = Trace.start(AGENT_NAME, self.backend.name, self.backend.model, question, self.max_iterations)
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        self._say(f"\n{'='*72}\n PremierPreneur | {AGENT_NAME} | backend={self.backend.name} model={self.backend.model}"
                  f"\n max_iterations={self.max_iterations}\n{'='*72}\n Question: {question}\n")

        for iteration in range(1, self.max_iterations + 1):
            try:
                result: ChatResult = self.backend.chat(messages, toolbox.TOOL_SCHEMAS)
            except Exception as e:  # network / auth / model errors end the run cleanly
                trace.finish("error", None, error=f"{type(e).__name__}: {e}")
                self._say(f"\n[error] {type(e).__name__}: {e}")
                return trace

            step = trace.new_step(iteration, result.content, result.latency_ms, result.usage)
            self._say(f"--- iteration {iteration}/{self.max_iterations} ({result.latency_ms:.0f} ms) ---")

            if not result.tool_calls:
                # the model answered -> natural stop condition
                trace.finish("final_answer", result.content)
                self._say(f" no tool call -> final answer\n\n--- final answer ---\n{result.content.strip()}\n")
                return trace

            if result.content:
                self._say(f" reasoning: {result.content.strip()}")

            messages.append(result.as_message())
            for tc in result.tool_calls:
                t0 = time.perf_counter()
                observation = toolbox.dispatch(tc.name, tc.arguments, self.db_path)
                latency = (time.perf_counter() - t0) * 1000
                is_error = isinstance(observation, dict) and "error" in observation
                step.tool_calls.append(_record(tc, observation, latency, is_error))
                self._say(f" tool call: {tc.name}({tc.arguments})")
                self._say(f" observation: {_preview(observation)}{'  <-- ERROR' if is_error else ''}")
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                                 "content": json.dumps(observation)})

        # loop fell through -> guard triggered
        msg = (f"Stopped by the max-iteration guard: {self.max_iterations} iterations used without a final answer. "
               f"Partial evidence is in the trace; no recommendation is issued.")
        trace.finish("max_iterations", msg)
        self._say(f"\n[guard] {msg}\n")
        return trace

    def _say(self, text: str) -> None:
        if self.verbose:
            print(text, flush=True)


def _record(tc, observation, latency_ms, is_error):
    from .trace import ToolCallRecord
    try:
        args = json.loads(tc.arguments) if tc.arguments.strip() else {}
    except json.JSONDecodeError:
        args = {"_raw": tc.arguments}
    return ToolCallRecord(call_id=tc.id, tool=tc.name, arguments=args, observation=observation,
                          latency_ms=latency_ms, is_error=is_error)


def _preview(obs, limit: int = 160) -> str:
    s = json.dumps(obs)
    return s if len(s) <= limit else s[:limit] + f"... ({len(s)} chars)"


def run_question(question: str, backend: str = "mock", model: str | None = None,
                 max_iterations: int = DEFAULT_MAX_ITERATIONS, verbose: bool = True, save: bool = True) -> Trace:
    """Convenience entry point used by the CLI and (later) the dashboard."""
    agent = BusinessAnalystAgent(make_backend(backend, model), max_iterations=max_iterations, verbose=verbose)
    trace = agent.run(question)
    if save:
        json_path, md_path = trace.save()
        if verbose:
            print(f"trace saved: {md_path}\n              {json_path}")
    return trace
