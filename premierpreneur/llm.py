"""
LLM backends behind one tiny interface: backend.chat(messages, tools) -> ChatResult.

    openai  - OpenAI GPT models via the official SDK (needs OPENAI_API_KEY)
    ollama  - any local Ollama model through its OpenAI-compatible endpoint
    mock    - a scripted model with no network; follows a fixed, sensible
              tool-calling policy so the loop, tools, guard and trace can be
              exercised offline (and demonstrated when the API is unavailable)

All backends speak the OpenAI chat-completions message format so the agent
loop never has to know which one it is talking to.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, exactly as the model produced it


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict | None = None
    latency_ms: float = 0.0

    def as_message(self) -> dict:
        """Assistant message to append to the conversation history."""
        msg: dict = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        return msg


# --------------------------------------------------------------------------- #
# OpenAI-compatible backends (OpenAI cloud + Ollama local)
# --------------------------------------------------------------------------- #
class OpenAICompatibleBackend:
    name = "openai"

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None,
                 temperature: float = 0.0):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise SystemExit("The `openai` package is required: pip install openai") from e
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if base_url is None and not key:
            raise SystemExit("OPENAI_API_KEY is not set. Export it, or use --backend mock / --backend ollama.")
        self.client = OpenAI(base_url=base_url, api_key=key or "not-needed")
        self.model = model
        self.temperature = temperature

    def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self.temperature,
        )
        latency = (time.perf_counter() - t0) * 1000
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
            if getattr(tc, "type", "function") == "function"
        ]
        usage = None
        if getattr(resp, "usage", None):
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }
        return ChatResult(content=msg.content or "", tool_calls=calls, usage=usage, latency_ms=latency)


class OllamaBackend(OpenAICompatibleBackend):
    name = "ollama"

    def __init__(self, model: str, host: str | None = None, temperature: float = 0.0):
        host = host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        super().__init__(model=model, base_url=host.rstrip("/") + "/v1", api_key="ollama", temperature=temperature)


# --------------------------------------------------------------------------- #
# Mock backend - deterministic, offline
# --------------------------------------------------------------------------- #
class MockBackend:
    """Scripted stand-in for the model.

    It reads the conversation so far, decides the next tool call the way a
    competent analyst would, and composes a final answer only from numbers it
    has actually observed in tool results. Used to test the loop offline and
    as a fallback demo when no API key is available.
    """
    name = "mock"
    model = "scripted-analyst-v1"

    _MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")
    _PCT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
    _PID = re.compile(r"\bP\d{3}\b")

    def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        t0 = time.perf_counter()
        question = next((m["content"] for m in messages if m["role"] == "user"), "")
        observed = self._observations(messages)
        done = [name for name, _ in observed]
        q = question.lower()

        # ---- discount scenario ------------------------------------------------
        if "discount" in q or "markdown" in q or "clearance" in q:
            pid = self._PID.search(question)
            pct = self._PCT.search(question)
            if "get_sales_summary" not in done:
                return self._call("Owner is asking about a discount. First I need current margins and velocity "
                                  "so I know what the discount is eating into.", "get_sales_summary", {"days": 30}, t0)
            if "evaluate_discount" not in done:
                if not pid or not pct:
                    return self._final("I need a product id (e.g. P010) and a discount percentage to evaluate "
                                       "this. Which product and what discount are you considering?", t0)
                return self._call(f"Margins observed. Now compute the breakeven for {pid.group()} at "
                                  f"{pct.group(1)}% with the deterministic tool instead of estimating it.",
                                  "evaluate_discount", {"product_id": pid.group(), "discount_pct": float(pct.group(1))}, t0)
            return self._final(self._discount_answer(observed), t0)

        # ---- restock scenario (default) ---------------------------------------
        if "get_sales_summary" not in done:
            return self._call("The owner wants a restock plan within a fixed budget. Step 1: pull 30-day sales "
                              "so I know which products are selling and what margin each one carries.",
                              "get_sales_summary", {"days": 30}, t0)
        if "get_inventory_status" not in done:
            return self._call("Sales observed. Step 2: check on-hand stock and days of cover to see which "
                              "products are closest to running out relative to their supplier lead time.",
                              "get_inventory_status", {}, t0)
        if "get_supplier_terms" not in done:
            return self._call("Three products are at stockout risk and one has no cost on file. Step 3: get "
                              "unit costs, MOQs and lead times before pricing any order.",
                              "get_supplier_terms", {}, t0)
        if "plan_restock" not in done:
            m = self._MONEY.search(question)
            budget = float(m.group(1).replace(",", "")) if m else 1000.0
            strategy = next((s for s in ("conservative", "aggressive", "moderate") if s in q), "moderate")
            return self._call(f"I have sales, inventory and supplier terms. Step 4: run the deterministic "
                              f"restock planner with budget ${budget:,.0f}, strategy '{strategy}', and let it "
                              f"do the arithmetic.", "plan_restock", {"budget": budget, "strategy": strategy}, t0)
        return self._final(self._restock_answer(observed), t0)

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _observations(messages: list[dict]) -> list[tuple[str, dict]]:
        """(tool_name, parsed_result) for every tool message, in order."""
        id_to_name: dict[str, str] = {}
        for m in messages:
            if m["role"] == "assistant":
                for tc in m.get("tool_calls") or []:
                    id_to_name[tc["id"]] = tc["function"]["name"]
        out = []
        for m in messages:
            if m["role"] == "tool":
                try:
                    out.append((id_to_name.get(m["tool_call_id"], "?"), json.loads(m["content"])))
                except json.JSONDecodeError:
                    out.append((id_to_name.get(m["tool_call_id"], "?"), {"raw": m["content"]}))
        return out

    def _call(self, reasoning: str, name: str, args: dict, t0: float) -> ChatResult:
        return ChatResult(content=reasoning,
                          tool_calls=[ToolCall(id=f"call_mock_{name}", name=name, arguments=json.dumps(args))],
                          latency_ms=(time.perf_counter() - t0) * 1000)

    @staticmethod
    def _final(text: str, t0: float) -> ChatResult:
        return ChatResult(content=text, latency_ms=(time.perf_counter() - t0) * 1000)

    @staticmethod
    def _restock_answer(observed: list[tuple[str, dict]]) -> str:
        plan = next(r for n, r in reversed(observed) if n == "plan_restock")
        if "error" in plan:
            return f"The planner rejected the request: {plan['error']}. Please restate the budget."
        lines = [
            f"**Restock recommendation ({plan['strategy']} strategy, budget ${plan['budget']:,.2f})**", "",
            f"Proposed purchase: ${plan['total_cost']:,.2f}, leaving ${plan['remaining_budget']:,.2f} on hand.", "",
        ]
        for line in plan["plan"]:
            lines.append(f"- **{line['product_id']} {line['product_name']}** - order {line['order_qty']} units "
                         f"(${line['line_cost']:,.2f}); cover goes from {line['days_of_cover_now']} to "
                         f"{line['days_of_cover_after_delivery']} days ({line['note']}).")
        if plan.get("unfunded"):
            gap = sum(u["additional_cost_to_fully_fund"] for u in plan["unfunded"])
            lines += ["", f"Not fully funded: {len(plan['unfunded'])} products are still short of the target cover; "
                          f"reaching it would take about ${gap:,.2f} more "
                          f"(full plan: ${plan['budget_needed_for_full_target_plan']:,.2f})."]
        if plan.get("excluded_missing_cost"):
            ids = ", ".join(e["product_id"] for e in plan["excluded_missing_cost"])
            lines += ["", f"Needs your input: {ids} could not be priced because the unit cost is missing from "
                          "the catalog. Add the cost and re-run if you want it included."]
        lines += ["", "Assumptions: " + "; ".join(plan["assumptions"]) + ".",
                  "", "This is a recommendation for your review - no order has been placed."]
        return "\n".join(lines)

    @staticmethod
    def _discount_answer(observed: list[tuple[str, dict]]) -> str:
        ev = next(r for n, r in reversed(observed) if n == "evaluate_discount")
        if "error" in ev:
            return f"I could not evaluate that discount: {ev['error']}"
        d, c, b = ev["at_discount"], ev["current"], ev["breakeven"]
        return (f"**{ev['product_id']} {ev['product_name']} at {d['discount_pct']:.0f}% off**\n\n"
                f"Price drops from ${ev['unit_price']} to ${d['new_price']}; unit margin falls from "
                f"${c['unit_margin']} to ${d['unit_margin']} ({c['margin_pct']}% -> {d['margin_pct']}%).\n"
                f"To keep last month's gross profit (${c['gross_profit_30d']}) you would need to sell "
                f"{b['units_needed_30d_to_hold_gross_profit']} units in 30 days versus {ev['units_sold_last_30d']} "
                f"sold - a {b['required_volume_lift_pct']}% lift. {ev['assumptions'][0]}.\n\n"
                "This is a recommendation for your review - no price change has been made.")


# --------------------------------------------------------------------------- #
def make_backend(name: str, model: str | None = None, **kw):
    name = name.lower()
    if name == "openai":
        return OpenAICompatibleBackend(model=model or os.environ.get("PREMIERPRENEUR_MODEL", "gpt-4o-mini"), **kw)
    if name == "ollama":
        return OllamaBackend(model=model or os.environ.get("PREMIERPRENEUR_MODEL", "llama3.1:8b"), **kw)
    if name == "mock":
        return MockBackend()
    raise SystemExit(f"unknown backend {name!r}; choose openai, ollama or mock")
