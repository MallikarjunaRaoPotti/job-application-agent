"""Shared OpenRouter client: forced tool calling, bounded retries, daily-limit and budget handling.

Nemotron has no JSON mode (free tier), so structured output is obtained by forcing a single tool
call and parsing its arguments. The client is thread-safe so scoring can run calls in parallel, and
it tracks the dollar cost OpenRouter reports so a run can be capped by spend as well as call count.
"""

from __future__ import annotations

import json
import re
import threading
import time

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "nvidia/nemotron-3.5-lightning"   # paid tier: no daily cap, ~$0.0005 per scored job
FREE_MIN_INTERVAL = 3.5   # free models allow ~20 req/min


def is_free(model: str) -> bool:
    return model.endswith(":free")


class OpenRouterError(RuntimeError):
    """A call failed or returned unusable output."""


class AuthError(OpenRouterError):
    """Bad or missing API key; never worth retrying."""


class DailyLimitReached(RuntimeError):
    """Stop calling for this run: free daily quota, call budget, spend cap, or no credits left."""


class OpenRouterClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, min_interval: float | None = None,
                 call_budget: int | None = None, spend_cap_usd: float | None = None):
        self.api_key, self.model = api_key, model
        self.min_interval = (FREE_MIN_INTERVAL if is_free(model) else 0.0) if min_interval is None else min_interval
        self.call_budget, self.spend_cap_usd = call_budget, spend_cap_usd
        self.calls = 0
        self.spent_usd = 0.0
        self._last_call = 0.0
        self._lock = threading.Lock()

    def _reserve_call(self) -> None:
        """Enforce budget, spend cap and pacing; count the call. Thread-safe."""
        with self._lock:
            if self.call_budget is not None and self.calls >= self.call_budget:
                raise DailyLimitReached(f"call budget of {self.call_budget} for this run used up")
            if self.spend_cap_usd is not None and self.spent_usd >= self.spend_cap_usd:
                raise DailyLimitReached(f"spend cap of ${self.spend_cap_usd:.2f} for this run reached")
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            self.calls += 1

    def _record_cost(self, data: dict) -> None:
        cost = (data.get("usage") or {}).get("cost")
        if isinstance(cost, (int, float)):
            with self._lock:
                self.spent_usd += float(cost)

    def call_tool(self, messages: list[dict], tool: dict, temperature: float = 0) -> dict:
        """Force one call of `tool` and return its parsed arguments."""
        name = tool["function"]["name"]
        body = {"model": self.model, "temperature": temperature, "messages": messages, "tools": [tool],
                "tool_choice": {"type": "function", "function": {"name": name}}}
        for attempt in range(1, 4):
            self._reserve_call()
            try:
                resp = requests.post(OPENROUTER_URL, json=body, timeout=180,
                                     headers={"Authorization": f"Bearer {self.api_key}"})
            except requests.RequestException as exc:
                if attempt == 3:
                    raise OpenRouterError(f"OpenRouter request failed: {exc.__class__.__name__}") from exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 402:
                raise DailyLimitReached("OpenRouter credits exhausted (HTTP 402); add credits to continue")
            if resp.status_code in (401, 403):
                raise AuthError(f"OpenRouter auth failed (HTTP {resp.status_code}); check OPENROUTER_API_KEY")
            if resp.status_code == 429:
                if re.search(r"per[- ]day|daily|free-models-per-day", resp.text, re.I):
                    raise DailyLimitReached("OpenRouter free-model daily limit reached")
                if attempt == 3:
                    raise OpenRouterError("OpenRouter rate limited (429) after retries")
                time.sleep(10 * attempt)
                continue
            if resp.status_code >= 500:
                if attempt == 3:
                    raise OpenRouterError(f"OpenRouter HTTP {resp.status_code} after retries")
                time.sleep(2 ** attempt)
                continue
            if not resp.ok:
                raise OpenRouterError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            self._record_cost(data)
            return parse_tool_response(data, name)
        raise AssertionError("unreachable")


def parse_tool_response(data: dict, tool_name: str) -> dict:
    if data.get("error"):
        raise OpenRouterError(f"OpenRouter error: {str(data['error'])[:200]}")
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError("response has no message") from exc
    for call in msg.get("tool_calls") or []:
        if call.get("function", {}).get("name") == tool_name:
            try:
                return json.loads(call["function"]["arguments"])
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise OpenRouterError("tool arguments are not valid JSON") from exc
    # Fallback: some models put the JSON in content instead of a tool call.
    m = re.search(r"\{.*\}", msg.get("content") or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise OpenRouterError(f"model did not call {tool_name}")
