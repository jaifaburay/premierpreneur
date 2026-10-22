"""
Execution trace: reasoning -> tool calls -> observations -> answer.

One Trace per agent run. Saved as JSON (complete, machine-readable) and
Markdown (readable, for the milestone submission).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

TRACES_DIR = Path(__file__).resolve().parent.parent / "traces"
MAX_OBS_CHARS_MD = 3500  # keep the markdown readable; the JSON always holds the full observation


@dataclass
class ToolCallRecord:
    call_id: str
    tool: str
    arguments: dict
    observation: dict
    latency_ms: float
    is_error: bool


@dataclass
class Step:
    iteration: int
    reasoning: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    llm_latency_ms: float = 0.0
    usage: dict | None = None


@dataclass
class Trace:
    run_id: str
    agent: str
    backend: str
    model: str
    question: str
    max_iterations: int
    started_at: str
    steps: list[Step] = field(default_factory=list)
    final_answer: str | None = None
    stop_reason: str | None = None    # "final_answer" | "max_iterations" | "error"
    iterations_used: int = 0
    total_latency_ms: float = 0.0
    error: str | None = None

    _t0: float = field(default_factory=time.perf_counter, repr=False)

    # ------------------------------------------------------------------ build
    @classmethod
    def start(cls, agent: str, backend: str, model: str, question: str, max_iterations: int) -> "Trace":
        now = datetime.now()
        return cls(
            run_id=now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}",
            agent=agent, backend=backend, model=model, question=question,
            max_iterations=max_iterations, started_at=now.isoformat(timespec="seconds"),
        )

    def new_step(self, iteration: int, reasoning: str, llm_latency_ms: float, usage: dict | None) -> Step:
        step = Step(iteration=iteration, reasoning=reasoning, llm_latency_ms=llm_latency_ms, usage=usage)
        self.steps.append(step)
        self.iterations_used = iteration
        return step

    def finish(self, stop_reason: str, final_answer: str | None, error: str | None = None) -> None:
        self.stop_reason = stop_reason
        self.final_answer = final_answer
        self.error = error
        self.total_latency_ms = (time.perf_counter() - self._t0) * 1000

    # ------------------------------------------------------------------- save
    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_t0", None)
        return d

    def save(self, directory: Path = TRACES_DIR) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"trace_{self.run_id}_{self.backend}"
        json_path = directory / f"{stem}.json"
        md_path = directory / f"{stem}.md"
        json_path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, md_path

    # --------------------------------------------------------------- markdown
    def to_markdown(self) -> str:
        n_calls = sum(len(s.tool_calls) for s in self.steps)
        n_errors = sum(1 for s in self.steps for c in s.tool_calls if c.is_error)
        tokens = sum((s.usage or {}).get("total_tokens", 0) for s in self.steps)
        lines = [
            "# PremierPreneur - Execution Trace",
            "",
            f"| | |", "|---|---|",
            f"| Run ID | `{self.run_id}` |",
            f"| Started | {self.started_at} |",
            f"| Agent | {self.agent} |",
            f"| Backend / model | {self.backend} / `{self.model}` |",
            f"| Max iterations (stop guard) | {self.max_iterations} |",
            f"| Iterations used | {self.iterations_used} |",
            f"| Tool calls | {n_calls} ({n_errors} returned an error) |",
            f"| Stop reason | **{self.stop_reason}** |",
            f"| Total wall time | {self.total_latency_ms/1000:.1f} s |",
            f"| Tokens (if reported) | {tokens or 'n/a'} |",
            "",
            "## Input",
            "",
            f"> {self.question}",
            "",
        ]
        for s in self.steps:
            lines += [f"## Iteration {s.iteration} / {self.max_iterations}", ""]
            if not s.tool_calls:
                lines += ["_No tool call - the model judged it had enough evidence and produced its final answer "
                          "(shown below). This is the natural stop condition._", ""]
                continue
            lines += ["**Reasoning**", "", s.reasoning.strip() or "_(model returned no reasoning text)_", ""]
            for c in s.tool_calls:
                lines += [
                    f"**Tool call** `{c.tool}({json.dumps(c.arguments)})`  ",
                    f"_{c.latency_ms:.0f} ms{'  - returned ERROR' if c.is_error else ''}_",
                    "",
                    "**Observation**",
                    "",
                    "```json",
                    _truncate(json.dumps(c.observation, indent=2), MAX_OBS_CHARS_MD),
                    "```",
                    "",
                ]
        lines += ["## Final answer", ""]
        if self.final_answer:
            lines += [self.final_answer.strip(), ""]
        if self.error:
            lines += [f"**Error:** {self.error}", ""]
        if self.stop_reason == "max_iterations":
            lines += [f"> Stopped by the max-iteration guard after {self.iterations_used} iterations.", ""]
        return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars - full observation in the JSON trace]"
