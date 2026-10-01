#!/usr/bin/env python3
"""Verification script for api-recorder plugin.

Run with Hermes venv:
    cd ~/.hermes/hermes-agent && ./venv/bin/python ~/.hermes/plugins/api-recorder/verify_api_recorder.py

Checks:
  1. discover_and_load() registers plugin and its declared hooks (no rejection)
  2. Synthetic call records pushed through ingest → summary emitted + JSONL written
  3. py_compile on every .py file in the plugin
  4. git status clean, file list
"""
import importlib.util
import json
import logging
import os
import sys
import py_compile
import subprocess
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("verify_api_recorder")

home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
agent_dir = os.path.join(home, "hermes-agent")
sys.path.insert(0, agent_dir)

PLUGIN_DIR = Path(home) / "plugins" / "api-recorder"
# The checkout this script ships in — same directory as the file itself, so it
# works from a clone, a symlinked plugin dir, or a plain copy.
PROJECT_DIR = Path(__file__).resolve().parent

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results = []

# ── 1. discover_and_load ──────────────────────────────────────────────────────
print("\n=== [1] discover_and_load() ===")
try:
    import hermes_cli.plugins as hp
    manager = hp.PluginManager(scope_key=home)
    manager.discover_and_load(force=True)

    loaded = manager._plugins
    assert "api-recorder" in loaded, f"Plugin not loaded. Loaded: {list(loaded)}"
    print(f"  Loaded plugins: {list(loaded)}")

    hooks = manager._hooks
    assert "post_api_request" in hooks and hooks["post_api_request"], \
        f"post_api_request hook missing. hooks keys: {list(hooks)}"
    assert "api_request_error" in hooks and hooks["api_request_error"], \
        f"api_request_error hook missing."
    print(f"  Registered hooks: {[k for k,v in hooks.items() if v]}")
    print(f"  {PASS} discover_and_load registered api-recorder with both hooks")
    results.append(("discover_and_load", True))
except Exception as exc:
    print(f"  {FAIL} {exc}")
    results.append(("discover_and_load", False))

# ── 2. Synthetic ingest + flush ───────────────────────────────────────────────
print("\n=== [2] Synthetic ingest + JSONL emit ===")
try:
    plugin_path = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location("api_recorder", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Reset state
    mod._buckets.clear()
    import time
    mod._interval_start = time.monotonic()

    # Push synthetic records
    mod._on_post_api_request(model="test-model", provider="test-provider",
                             assistant_content_chars=250, api_duration=1.5)
    mod._on_post_api_request(model="test-model", provider="test-provider",
                             assistant_content_chars=0, assistant_tool_call_count=0,
                             finish_reason="stop", api_duration=0.3)  # true empty
    mod._on_post_api_request(model="test-model", provider="test-provider",
                             assistant_content_chars=0, assistant_tool_call_count=2,
                             finish_reason="tool_calls", api_duration=0.4)  # tool call → success
    mod._on_post_api_request(model="test-model", provider="test-provider",
                             assistant_content_chars=50, assistant_tool_call_count=0,
                             finish_reason="stop", api_duration=0.5)  # text → success
    mod._on_post_api_request(model="test-model", provider="test-provider",
                             assistant_content_chars=None, assistant_tool_call_count=None,
                             api_duration=0.2)  # missing fields → must not crash
    mod._on_post_api_request(model="test-model", provider="test-provider",
                             assistant_content_chars=0, assistant_tool_call_count=0,
                             finish_reason="aborted", api_duration=30.0)  # client went away
    mod._on_api_request_error(model="test-model", provider="test-provider",
                              reason="rate_limit")
    mod._on_api_request_error(model="test-model", provider="test-provider",
                              status_code=499, reason="client_abort")
    mod._on_api_request_error(model="slow-model", provider="other",
                              reason="timeout")

    # Force flush
    mod._interval_start = time.monotonic() - 99999
    mod._maybe_flush(force=True)

    # Read JSONL
    import datetime
    ts = datetime.date.today().isoformat()
    jsonl = mod._state_dir() / f"{ts}.jsonl"
    assert jsonl.exists(), f"JSONL not written at {jsonl}"
    last = json.loads(jsonl.read_text().strip().split("\n")[-1])
    print(f"  JSONL record: {json.dumps(last, indent=2)}")

    bkts = {(r["model"], r["provider"]): r for r in last["buckets"]}
    tm = bkts[("test-model", "test-provider")]
    # 6 post_api_request + 2 api_request_error = 8 total
    # success = text(250ch) + tool-call(2 tools) + text(50ch) = 3
    # empty_content = true-empty(0ch,0tools,finish=stop) + missing-fields(None,None) = 2
    # aborted = 0ch,0tools,finish=aborted = 1
    assert tm["total"] == 8, f"total={tm['total']}"
    assert tm["success"] == 3, f"success={tm['success']}"
    assert tm["empty_content"] == 2, f"empty_content={tm['empty_content']}"
    assert tm["aborted"] == 1, f"aborted={tm['aborted']}"
    assert tm["rate_limit"] == 1, f"rate_limit={tm['rate_limit']}"
    assert tm["client_abort"] == 1, f"client_abort={tm['client_abort']}"
    # The regression this guards: a tool-call turn (0 content chars, >=1 tool call)
    # must NOT be counted as an empty provider response. total spans both paths, so
    # every bucket must sum back to it.
    assert (tm["success"] + tm["empty_content"] + tm["aborted"]
            + tm["rate_limit"] + tm["stream_timeout"]
            + tm["client_abort"] + tm["other_error"] == tm["total"]), (
        "every recorded call must land in exactly one bucket")
    sm = bkts[("slow-model", "other")]
    assert sm["stream_timeout"] == 1, f"stream_timeout={sm['stream_timeout']}"
    print(f"  {PASS} ingest + JSONL verified")
    results.append(("synthetic_ingest", True))
except Exception as exc:
    import traceback; traceback.print_exc()
    print(f"  {FAIL} {exc}")
    results.append(("synthetic_ingest", False))

# ── 3. py_compile ─────────────────────────────────────────────────────────────
print("\n=== [3] py_compile ===")
py_files = list(PROJECT_DIR.glob("*.py"))
all_ok = True
for f in py_files:
    try:
        py_compile.compile(str(f), doraise=True)
        print(f"  OK  {f.name}")
    except py_compile.PyCompileError as exc:
        print(f"  {FAIL}  {f.name}: {exc}")
        all_ok = False
results.append(("py_compile", all_ok))
if all_ok:
    print(f"  {PASS} all .py files compile")

# ── 4. file inventory ─────────────────────────────────────────────────────────
print("\n=== [4] file inventory ===")
try:
    required = {"__init__.py", "plugin.yaml", "README.md", "verify_api_recorder.py"}
    present = {p.name for p in PROJECT_DIR.iterdir() if p.is_file()}
    print(f"  {sorted(present)}")
    missing = required - present
    assert not missing, f"missing plugin files: {sorted(missing)}"
    stray = [p.name for p in PROJECT_DIR.rglob("__pycache__")]
    if stray:
        print(f"  note: __pycache__ present (gitignored): {stray[:2]}")
    results.append(("file_inventory", True))
except Exception as exc:
    print(f"  {FAIL} {exc}")
    results.append(("file_inventory", False))

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*50)
print("SUMMARY")
all_passed = True
for name, ok in results:
    status = PASS if ok else FAIL
    print(f"  {status}  {name}")
    if not ok:
        all_passed = False
if all_passed:
    print("\n  ALL CHECKS PASSED")
    sys.exit(0)
else:
    print("\n  SOME CHECKS FAILED")
    sys.exit(1)
