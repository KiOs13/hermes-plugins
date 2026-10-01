"""Hermes plugin: api-recorder — observe model API calls and report health.

Hooks:
  post_api_request  — successful call (HTTP 200 with response body)
  api_request_error — failed attempt

Emits a summary every API_RECORDER_INTERVAL seconds (default 600) at WARNING
level (visible in journalctl) and appends one JSON line per interval to
~/.hermes/cache/api-recorder/YYYY-MM-DD.jsonl.

Measures per (model, provider):
  - total, success
  - empty_content (HTTP 200, no content AND no tool calls, finish_reason not abort-shaped)
  - aborted (HTTP 200, no content AND no tool calls, finish_reason says the call was
    aborted/cancelled — the client went away mid-call; 499 itself only reaches us via
    the error path, since post_api_request carries no status_code)
  - rate_limit (429 / reason=="rate_limit")
  - stream_timeout (504 / reason=="stream_timeout")
  - client_abort (499 / reason=="client_abort")
  - other_error (non-2xx not in above buckets)
  - latency stats (count, min, avg, approx-p95, max) from api_duration

Design constraints: fail-open, unbounded-memory-safe (flush + reset each
interval), no real provider traffic, no extra deps beyond stdlib.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── bucket key ────────────────────────────────────────────────────────────────

def _key(kw: dict) -> tuple[str, str]:
    return (kw.get("model") or "unknown", kw.get("provider") or "unknown")


# ── latency reservoir (approx p95 via sort-on-flush, bounded 1000 samples) ───
# ponytail: capped list, upgrade to t-digest if >1k samples/interval matters

_MAX_LATENCY_SAMPLES = 1000


class _LatencyBuf:
    __slots__ = ("samples",)

    def __init__(self) -> None:
        self.samples: list[float] = []

    def record(self, v: float) -> None:
        if len(self.samples) < _MAX_LATENCY_SAMPLES:
            self.samples.append(v)

    def stats(self) -> dict | None:
        s = self.samples
        if not s:
            return None
        s_sorted = sorted(s)
        n = len(s_sorted)
        p95_idx = min(int(math.ceil(0.95 * n)) - 1, n - 1)
        return {
            "n": n,
            "min": round(s_sorted[0], 3),
            "avg": round(sum(s_sorted) / n, 3),
            "p95": round(s_sorted[p95_idx], 3),
            "max": round(s_sorted[-1], 3),
        }


# ── per-bucket counters ────────────────────────────────────────────────────────

# ponytail: the only abort-ish signals actually present on the post_api_request payload
# (it carries finish_reason, no status_code). 499 itself arrives via api_request_error.
_ABORT_FINISH_REASONS = {"error", "abort", "aborted", "cancelled", "canceled", "client_abort"}


def _classify_post(kw: dict) -> str:
    """Return 'success' | 'aborted' | 'empty_content' for a post_api_request event.

    A tool-call turn legitimately has zero content chars — only a response with
    neither content nor tool calls is "empty".
    """
    chars = kw.get("assistant_content_chars") or 0
    tools = kw.get("assistant_tool_call_count") or 0
    if chars or tools:
        return "success"
    if str(kw.get("finish_reason") or "").strip().lower() in _ABORT_FINISH_REASONS:
        return "aborted"
    return "empty_content"


class _Bucket:
    __slots__ = ("total", "success", "empty_content", "aborted",
                 "rate_limit", "stream_timeout", "client_abort", "other_error",
                 "lat")

    def __init__(self) -> None:
        self.total = 0
        self.success = 0
        self.empty_content = 0
        self.aborted = 0
        self.rate_limit = 0
        self.stream_timeout = 0
        self.client_abort = 0
        self.other_error = 0
        self.lat = _LatencyBuf()

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "total": self.total,
            "success": self.success,
            "empty_content": self.empty_content,
            "aborted": self.aborted,
            "rate_limit": self.rate_limit,
            "stream_timeout": self.stream_timeout,
            "client_abort": self.client_abort,
            "other_error": self.other_error,
        }
        lat = self.lat.stats()
        if lat:
            d["latency_s"] = lat
        return d


# ── shared state ───────────────────────────────────────────────────────────────

_lock = threading.Lock()  # ponytail: one global lock, fine for low-freq hook calls
_buckets: dict[tuple[str, str], _Bucket] = defaultdict(_Bucket)
_interval_start: float = time.monotonic()


def _get_bucket(kw: dict) -> _Bucket:
    return _buckets[_key(kw)]


# ── output helpers ─────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    d = Path(home) / "cache" / "api-recorder"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _flush(buckets: dict, interval_start: float, interval_end: float) -> None:
    """Emit summary log + append JSONL line. Never raises."""
    if not buckets:
        return

    elapsed = round(interval_end - interval_start, 1)
    lines = [f"api-recorder: interval={elapsed}s buckets={len(buckets)}"]
    rows = []
    for (model, provider), b in sorted(buckets.items()):
        d = b.as_dict()
        lines.append(
            f"  {model}/{provider}: total={d['total']} ok={d['success']}"
            f" empty={d['empty_content']} abrt={d['aborted']} rl={d['rate_limit']}"
            f" timeout={d['stream_timeout']} cabort={d['client_abort']}"
            f" err={d['other_error']}"
            + (f" lat={d['latency_s']}" if "latency_s" in d else "")
        )
        rows.append({"model": model, "provider": provider, **d})

    try:
        logger.warning("\n".join(lines))
    except Exception as exc:
        logger.debug("api-recorder: log emit failed: %s", exc)

    try:
        ts = time.strftime("%Y-%m-%d", time.localtime(interval_end))
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(interval_end)),
            "interval_s": elapsed,
            "buckets": rows,
        }
        path = _state_dir() / f"{ts}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug("api-recorder: jsonl write failed: %s", exc)


def _maybe_flush(force: bool = False) -> None:
    """Check if interval elapsed; if so, snapshot + reset state, then flush outside lock."""
    global _interval_start

    interval = int(os.environ.get("API_RECORDER_INTERVAL", "600"))
    now = time.monotonic()

    with _lock:
        if not force and (now - _interval_start) < interval:
            return
        # Snapshot and reset atomically
        snapshot = dict(_buckets)
        t_start = _interval_start
        _buckets.clear()
        _interval_start = now

    # Flush outside lock so file I/O never blocks hook callbacks
    _flush(snapshot, t_start, time.time())


# ── hook callbacks ─────────────────────────────────────────────────────────────

def _on_post_api_request(**kw: Any) -> None:
    try:
        with _lock:
            b = _get_bucket(kw)
            b.total += 1
            verdict = _classify_post(kw)
            if verdict == "success":
                b.success += 1
            elif verdict == "aborted":
                b.aborted += 1
            else:
                b.empty_content += 1
            dur = kw.get("api_duration")
            if isinstance(dur, (int, float)) and dur >= 0:
                b.lat.record(float(dur))
    except Exception as exc:
        logger.debug("api-recorder: post_api_request ingest error: %s", exc)
    _maybe_flush()


def _on_api_request_error(**kw: Any) -> None:
    try:
        status = kw.get("status_code") or 0
        reason = (kw.get("reason") or "").lower()
        with _lock:
            b = _get_bucket(kw)
            b.total += 1
            if status == 429 or "rate_limit" in reason:
                b.rate_limit += 1
            elif status in (408, 504) or reason == "timeout":
                b.stream_timeout += 1
            elif status == 499:
                b.client_abort += 1
            else:
                b.other_error += 1
    except Exception as exc:
        logger.debug("api-recorder: api_request_error ingest error: %s", exc)
    _maybe_flush()


# ── register ───────────────────────────────────────────────────────────────────

def register(ctx: Any) -> None:
    """Called once by Hermes PluginManager at startup. Never raises."""
    try:
        ctx.register_hook("post_api_request", _on_post_api_request)
        ctx.register_hook("api_request_error", _on_api_request_error)
        interval = int(os.environ.get("API_RECORDER_INTERVAL", "600"))
        logger.warning(
            "api-recorder: registered hooks post_api_request + api_request_error, "
            "interval=%ds, state_dir=%s",
            interval,
            _state_dir(),
        )
    except Exception as exc:
        logger.warning("api-recorder: register failed: %s", exc)


# ── self-check (runnable: python __init__.py) ──────────────────────────────────

def _demo() -> None:
    """Push synthetic records and emit one flush. Exits non-zero on assertion failure."""
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    import io
    import sys

    # Inject a fake ctx
    class _Ctx:
        def register_hook(self, name, cb):
            print(f"[demo] registered hook: {name}")

    register(_Ctx())

    # Synthetic calls
    _on_post_api_request(model="gpt-4o", provider="openai",
                         assistant_content_chars=512, api_duration=1.2)
    _on_post_api_request(model="gpt-4o", provider="openai",
                         assistant_content_chars=0, assistant_tool_call_count=0,
                         finish_reason="stop", api_duration=0.8)  # true empty
    _on_post_api_request(model="gpt-4o", provider="openai",
                         assistant_content_chars=0, assistant_tool_call_count=1,
                         finish_reason="tool_calls", api_duration=0.9)  # tool call → success
    _on_post_api_request(model="gpt-4o", provider="openai",
                         assistant_content_chars=0, assistant_tool_call_count=0,
                         finish_reason="aborted", api_duration=30.0)  # client went away
    _on_post_api_request(model="claude-3-opus", provider="anthropic",
                         assistant_content_chars=100, api_duration=2.5)
    _on_api_request_error(model="gpt-4o", provider="openai", reason="rate_limit")
    _on_api_request_error(model="claude-3-opus", provider="anthropic",
                          reason="timeout")

    # Force flush
    global _interval_start
    _interval_start = time.monotonic() - 9999
    _maybe_flush(force=True)

    # Verify JSONL written
    import datetime
    ts = datetime.date.today().isoformat()
    path = _state_dir() / f"{ts}.jsonl"
    assert path.exists(), f"JSONL not written: {path}"
    last_line = path.read_text().strip().split("\n")[-1]
    record = json.loads(last_line)
    assert len(record["buckets"]) == 2, f"Expected 2 buckets, got: {record['buckets']}"
    gpt = next(r for r in record["buckets"] if r["model"] == "gpt-4o")
    # 4 post_api_request (512ch, true-empty, tool-call, aborted) + 1 error
    assert gpt["total"] == 5, f"total wrong: {gpt}"
    assert gpt["success"] == 2, f"success wrong: {gpt}"          # 512ch + tool-call
    assert gpt["empty_content"] == 1, f"empty_content wrong: {gpt}"
    assert gpt["aborted"] == 1, f"aborted wrong: {gpt}"
    assert gpt["rate_limit"] == 1, f"rate_limit wrong: {gpt}"
    anthropic = next(r for r in record["buckets"] if r["model"] == "claude-3-opus")
    assert anthropic["stream_timeout"] == 1, f"stream_timeout wrong: {anthropic}"
    print(f"\n[demo] JSONL line:\n{last_line}")
    print("\n[demo] ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    _demo()
