"""HTTP client for the VMI API, with a record of everything it sent.

The console talks to the service the same way any other consumer does — over
HTTP, never by importing the pipeline. If something is awkward to render here,
the API is missing something, and that is worth finding out now rather than when
the real trading system tries to consume it.

Every call is captured in `exchanges` so the Processing tab can show the raw
traffic without each method remembering to report itself.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8100"
MAX_BODY_CHARS = 20_000

ANALYSIS_TIMEOUT = 900.0
"""Three vision calls against a local model on a CPU. The default 5 seconds
would lose every time."""

REPLAY_TIMEOUT = 7200.0
"""A replay is one analysis per cursor. Two hours is a short one."""

PULL_TIMEOUT = 3600.0
"""Downloading weights is gigabytes over a home connection."""


class ApiError(RuntimeError):
    """The API answered with an error status, or could not be reached."""

    def __init__(self, status_code: int, detail: str, request_id: str = "") -> None:
        suffix = f" (request {request_id})" if request_id else ""
        super().__init__(f"HTTP {status_code}: {detail}{suffix}")
        self.status_code = status_code
        self.detail = detail
        self.request_id = request_id


class VmiClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(base_url=self._base_url, headers=headers, timeout=30.0)
        self.exchanges: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        exchange: dict[str, Any] = {
            "method": method,
            "url": f"{self._base_url}{path}",
            "request": kwargs.get("json") or kwargs.get("params") or {},
            "timeout_s": kwargs.get("timeout"),
        }
        self.exchanges.append(exchange)
        started = time.perf_counter()

        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            exchange["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
            exchange["status"] = 0
            exchange["error"] = str(exc)
            raise ApiError(0, f"cannot reach the API at {self._base_url}: {exc}") from exc

        exchange["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        exchange["status"] = response.status_code
        exchange["request_id"] = response.headers.get("X-Request-ID", "")
        exchange["bytes"] = len(response.content)

        if response.status_code >= 400:
            exchange["error"] = _detail(response)
            raise ApiError(
                response.status_code, _detail(response), response.headers.get("X-Request-ID", "")
            )

        if response.headers.get("content-type", "").startswith("image/"):
            return response.content

        body = response.json()
        exchange["response"] = body
        return body

    # ------------------------------------------------------------------ calls

    def health(self) -> dict[str, Any]:
        return dict(self._request("GET", "/health"))

    def config(self) -> dict[str, Any]:
        return dict(self._request("GET", "/config"))

    def analyze(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
        as_of: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        data_provider: str | None = None,
        include_charts: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "timeframes": timeframes,
            "as_of": as_of,
            "provider": provider,
            "model": model,
            "data_provider": data_provider,
            "include_charts": include_charts,
            "store": True,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        return dict(self._request("POST", "/analyze", json=payload, timeout=ANALYSIS_TIMEOUT))

    def analyze_charts(self, symbol: str, charts: dict[str, str]) -> dict[str, Any]:
        payload = {"symbol": symbol, "charts": charts}
        return dict(
            self._request("POST", "/analyze/charts", json=payload, timeout=ANALYSIS_TIMEOUT)
        )

    def models(self, provider: str | None = None, free_only: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if provider:
            params["provider"] = provider
        if free_only:
            params["free_only"] = True
        return dict(self._request("GET", "/models", params=params))

    def select_model(self, provider: str, model: str) -> dict[str, Any]:
        return dict(
            self._request("POST", "/models/select", json={"provider": provider, "model": model})
        )

    def pull_model(self, model: str, provider: str = "ollama") -> dict[str, Any]:
        return dict(
            self._request(
                "POST",
                "/models/pull",
                json={"model": model, "provider": provider},
                timeout=PULL_TIMEOUT,
            )
        )

    def runs(self, limit: int = 50, symbol: str | None = None, state: str | None = None):
        params: dict[str, Any] = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        if state:
            params["state"] = state
        return dict(self._request("GET", "/runs", params=params))

    def run(self, run_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/runs/{run_id}"))

    def chart(self, run_id: str, timeframe: str) -> bytes:
        return bytes(self._request("GET", f"/runs/{run_id}/chart/{timeframe}"))

    def delete_run(self, run_id: str) -> dict[str, Any]:
        return dict(self._request("DELETE", f"/runs/{run_id}"))

    def replay(self, symbol: str, start: str, end: str, step: str = "24h") -> dict[str, Any]:
        payload = {"symbol": symbol, "start": start, "end": end, "step": step, "store": True}
        return dict(self._request("POST", "/replay", json=payload, timeout=REPLAY_TIMEOUT))

    def search_symbols(self, query: str) -> dict[str, Any]:
        return dict(self._request("GET", "/symbols/search", params={"q": query}))


def _detail(response: httpx.Response) -> str:
    """Pull a readable message out of whichever error shape came back."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or response.reason_phrase

    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    if isinstance(detail, list):  # FastAPI validation errors
        return "; ".join(str(item.get("msg", item)) for item in detail)
    return str(detail)
