# api-recorder

Hermes Agent plugin that **observes** model API calls and periodically reports
their health. It changes no behaviour — it is a measurement tool only.

---

## Why it exists

Two classes of failure are otherwise invisible in production:

1. **HTTP 200 with empty assistant content** — the provider returns a success
   response but the message body is blank. Hermes sees a valid reply; nothing
   in journalctl shows a problem. These accumulate silently.

2. **Daily free-tier rate limits (429)** — logged per-attempt by the error
   classifier, but not aggregated across a session or day. Without bucketed
   counts you cannot tell whether you hit the limit once or fifty times.

Additionally, stream timeouts (504) and client-aborted connections (499) often
look identical in the agent log — both surface as a retry — but have different
root causes (provider slowness vs. network interruption).

api-recorder surfaces all four as named counters per model/provider, plus a
split of everything else the error classifier can report — `auth`, `billing`,
`overloaded`, `server_error`, `context_overflow`, `policy_blocked`, `tls_error`
— so a failure you cannot explain lands in `other_error` as the only remaining
unknown, not as one entry among dozens of causes.

---

## What it measures (per model × provider bucket)

| Counter | Meaning |
|---|---|
| `total` | Every `post_api_request` + `api_request_error` event |
| `success` | `post_api_request` with content chars **or** tool calls (`assistant_tool_call_count > 0`) |
| `empty_content` | `post_api_request` with **no** content chars **and** **no** tool calls — provider answered with nothing |
| `aborted` | `post_api_request` with no content and no tool calls **and** an abort-shaped `finish_reason` (`error`/`abort`/`aborted`/`cancelled`/`canceled`/`client_abort`) — client went away mid-call. The success hook carries no `status_code`, so a real HTTP 499 can only appear via the error path; see caveat below. |
| `rate_limit` | `api_request_error` where reason contains `rate_limit` / `429` |
| `stream_timeout` | `api_request_error` where reason contains `stream_timeout` / `504` |
| `client_abort` | `api_request_error` where reason contains `client_abort` / `499` |
| `auth` | `api_request_error` with `reason` `auth` or `auth_permanent` — credentials rejected (401/403), refresh/rotate the key |
| `billing` | `api_request_error` with `reason` `billing` — 402 or confirmed credit exhaustion, rotate to a funded account |
| `overloaded` | `api_request_error` with `reason` `overloaded` — 503/529, provider busy, backoff |
| `server_error` | `api_request_error` with `reason` `server_error` — 500/502, plain retry |
| `context_overflow` | `api_request_error` with `reason` `context_overflow`, `payload_too_large`, `long_context_tier` or `oauth_long_context_beta_forbidden` — request too big, shrink it |
| `policy_blocked` | `api_request_error` with `reason` `content_policy_blocked`, `provider_policy_blocked`, `model_entitlement`, `model_not_found` or `upstream_blocked` — this request/model/account is refused; change one of them, retrying unchanged is pointless |
| `tls_error` | `api_request_error` with `reason` `ssl_cert_verification` — deterministic cert-chain failure, fails fast |
| `other_error` | Any other `api_request_error`. **Catch-all on purpose** — it is never removed, so a new `FailoverReason` member can never be silently dropped |
| `latency_s` | `{n, min, avg, p95, max}` from `api_duration` (seconds); omitted if no successful timing |

The named error counters are keyed on the **recovery action** Hermes takes
(`agent/error_classifier.py` → `FailoverReason`, 29 members), not on the HTTP
code, because two different reasons with the same status code need different
fixes. `verify_api_recorder.py` sweeps all 29 enum members and asserts each one
lands in exactly one counter, and that `REASON_COUNTER` never names a member
that no longer exists. Members sharing one recovery action share one counter;
anything unmapped stays in `other_error`.

Anything that reached `other_error` before this split cannot be re-labelled
retroactively — the old JSONL only stored the count. Counters split from the
next flush onward.

Latency samples are capped at 1 000 per interval; p95 is exact within that
window. Counters are reset to zero after every flush — no cumulative totals,
no time-series database.

---

## Installation

```
cp -r /path/to/api-recorder-plugin ~/.hermes/plugins/api-recorder
```

Or symlink (survives plugin edits without re-copying):

```
ln -s "$(pwd)" ~/.hermes/plugins/api-recorder
```

Restart the Hermes service:

```
systemctl --user restart hermes-gateway
```

Verify registration:

```console
$ journalctl --user -u hermes-gateway -n 50 | grep api-recorder
... WARNING ... api-recorder: registered hooks post_api_request + api_request_error, interval=600s, state_dir=...
```

---

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `API_RECORDER_INTERVAL` | `600` | Flush interval in seconds (10 min). Set to `60` for quick testing. |
| `HERMES_HOME` | `~/.hermes` | Where to write JSONL files (`$HERMES_HOME/cache/api-recorder/`). |

---

### The three "no output" counters

| | Source hook | Trigger | Who caused it |
|---|---|---|---|
| `empty_content` | `post_api_request` | no content, no tool calls, normal `finish_reason` (`stop`/`length`/`tool_calls`/missing) | provider — it answered with an empty body |
| `aborted` | `post_api_request` | no content, no tool calls, abort-shaped `finish_reason` | client — connection went away mid-call |
| `client_abort` | `api_request_error` | HTTP 499 (`status_code == 499`) | client, reported as a failure |

`aborted` and `client_abort` describe the same root cause from two different
hook surfaces. They are deliberately separate: a reader must be able to see
"the client keeps hanging up" (`client_abort`, hard failures) versus "calls
return 200 but deliver nothing because the client vanished" (`aborted`).

`post_api_request` does **not** carry `status_code` (checked in
`agent/turn_response_intake.py::_fire_post_api_request_hook` and
`agent/api_request_hooks.py::_invoke_api_request_error_hook` — only the error
hook has it). So `aborted` cannot test for 499; it tests only keys that really
exist on that payload: `finish_reason`, `assistant_content_chars`,
`assistant_tool_call_count`.

---

## Reading the output

### Journal (WARNING level)

Verbatim self-test output (it forces a flush by rewinding the interval clock,
so its `interval=` is a raw monotonic reading, not 600 — real flushes print
elapsed seconds):

```
WARNING api-recorder: interval=1790896115.5s buckets=3
  claude-3-opus/anthropic: total=2 ok=1 empty=0 abrt=0 stream_timeout=1 lat={'n': 1, 'min': 2.5, 'avg': 2.5, 'p95': 2.5, 'max': 2.5}
  err-model/err-provider: total=7 ok=0 empty=0 abrt=0 auth=1 billing=1 overloaded=1 server_error=1 context_overflow=1 policy_blocked=1 tls_error=1
  gpt-4o/openai: total=5 ok=2 empty=1 abrt=1 rate_limit=1 lat={'n': 4, 'min': 0.8, 'avg': 8.225, 'p95': 30.0, 'max': 30.0}
```

Only non-zero error counters are printed (`err=none` when there were no
failures at all), so the list after `abrt=` is exactly the list of things that
went wrong.

```bash
journalctl --user -u hermes-gateway -p warning -g api-recorder
```

### JSONL file (`~/.hermes/cache/api-recorder/YYYY-MM-DD.jsonl`)

One line per flush interval. Each line is a JSON object:

```json
{"ts":"2026-10-01T12:00:00Z","interval_s":600.0,"buckets":[
  {"model":"gpt-4o","provider":"openai","total":5,"success":2,"empty_content":1,"aborted":1,"rate_limit":1,"stream_timeout":0,"client_abort":0,"auth":0,"billing":0,"overloaded":0,"server_error":0,"context_overflow":0,"policy_blocked":0,"tls_error":0,"other_error":0,"latency_s":{"n":4,"min":0.8,"avg":8.225,"p95":30.0,"max":30.0}},
  {"model":"err-model","provider":"err-provider","total":7,"success":0,"empty_content":0,"aborted":0,"rate_limit":0,"stream_timeout":0,"client_abort":0,"auth":1,"billing":1,"overloaded":1,"server_error":1,"context_overflow":1,"policy_blocked":1,"tls_error":1,"other_error":0}
]}
```

Every counter is always present, zeroed or not — a missing key would make
"no `auth` failures" indistinguishable from "old plugin version".

Parse with any JSON tool:

```bash
# Last flush for today
tail -1 ~/.hermes/cache/api-recorder/$(date +%Y-%m-%d).jsonl | python3 -m json.tool

# All empty-content events across all days
cat ~/.hermes/cache/api-recorder/*.jsonl | \
  python3 -c "import sys,json; [print(r) for l in sys.stdin for r in json.loads(l)['buckets'] if r['empty_content']>0]"
```

---

## Caveats

- **Observer only** — this plugin records what the hook surface reports; it
  cannot fix failures or change retry logic.
- **Provider-level visibility** — the `reason` field in `api_request_error` is
  whatever Hermes's error classifier produces. If the classifier mis-labels a
  529 as a generic error, so will this plugin.
- **Counters reset each interval** — no persistence across restarts beyond the
  JSONL file. If the service restarts mid-interval, that interval's counts are
  lost.
- **Latency is from the agent's perspective** — includes connection overhead,
  streaming time, and any Hermes-side buffering. It is not provider TTFT.
- **No memory growth** — the plugin holds at most `buckets × 1 000` latency
  floats in RAM. At 64 bytes per float that is ≤ 64 KB for ten distinct models.

---

## Self-test

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python ~/.hermes/plugins/api-recorder/__init__.py
```

Expected: `[demo] ALL ASSERTIONS PASSED` after printing the JSONL record.

Full check (registration, ingest, compile, file inventory):

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python ~/.hermes/plugins/api-recorder/verify_api_recorder.py
```

Expected: `ALL CHECKS PASSED`. Check [2] also sweeps all 29 `FailoverReason`
members and prints where each one lands, so a new enum member in core shows up
here before it can hide in `other_error`:

```
FailoverReason sweep (29 members):
    billing            1  billing
    overloaded         1  overloaded
    server_error       1  server_error
    stream_timeout     1  timeout (pre-existing HTTP branch)
    tls_error          1  ssl_cert_verification
    auth               2  auth, auth_permanent
    rate_limit         2  rate_limit, upstream_rate_limit (pre-existing HTTP branch)
    context_overflow   4  context_overflow, long_context_tier, oauth_long_context_beta_forbidden, payload_too_large
    policy_blocked     5  content_policy_blocked, model_entitlement, model_not_found, provider_policy_blocked, upstream_blocked
    other_error       11  format_error, image_corrupt, image_too_large, incomplete_response, invalid_encrypted_content, llama_cpp_grammar_pattern, multimodal_tool_content_unsupported, reasoning_mandatory, role_alternation, thinking_signature, unknown
```

## Report

`api_recorder_report.py` reads the JSONL files and prints per-model health:

```bash
python3 ~/.hermes/plugins/api-recorder/api_recorder_report.py
python3 ~/.hermes/plugins/api-recorder/api_recorder_report.py --hours 6
python3 ~/.hermes/plugins/api-recorder/api_recorder_report.py --with-synthetic
```

Sample output (real run of this script against a real data dir, model and
provider names genericised):

```
api-recorder report  |  14 intervals  |  2026-10-01T17:50:10Z .. 2026-10-01T20:06:44Z

model                     prov          tot   ok  EMPTY  avg_s  errors                                         alert
--------------------------------------------------------------------------------------------------------------------
worker combo              custom:stac   763  760      1   12.6  oth=2                                          EMPTY=1
gateway combo             custom         90   90      0   10.9  -                                              
review combo              custom         50   50      0    5.8  -                                              
main combo                custom         28   28      0   10.2  -                                              
worker combo              custom:stac     7    0      0    0.0  auth=1 bill=1 ovl=1 5xx=1 ctx=1 pol=1 tls=1    auth=1 bill=1 pol=1 tls=1

total calls: 938
  success             928  (98.9%)
  empty_content         1  (0.1%)
  auth                  1  (0.1%)
  billing               1  (0.1%)
  overloaded            1  (0.1%)
  server_error          1  (0.1%)
  context_overflow      1  (0.1%)
  policy_blocked        1  (0.1%)
  tls_error             1  (0.1%)
  other_error           2  (0.2%)

2 model bucket(s) with alert-worthy counters (auth, billing, empty_content, policy_blocked, rate_limit, stream_timeout, tls_error).
```

The `errors` cell lists only the counters that actually fired, so a wide table
is not needed to see them: `auth=1 bill=1 pol=1 tls=1` reads directly as
"credentials failed, credit ran out, a model got blocked, and a cert chain
broke". Short codes: `429` rate limit, `to` stream timeout, `499` client abort,
`auth`, `bill` billing, `ovl` overloaded, `5xx` server error, `ctx` context too
big, `pol` policy/model blocked, `tls` cert chain, `oth` unclassified.

The `alert` column repeats only the counters from `ALERT` — `empty_content`,
`rate_limit`, `stream_timeout`, `auth`, `billing`, `policy_blocked`, `tls_error` —
because those need a human; `overloaded` and `server_error` clear up on their
own, so they show in `errors` but do not raise a flag. The script exits non-zero
and says so plainly if the plugin has recorded nothing, rather than printing an
empty table that reads like "all fine".

Records from the plugin's own self-test are skipped by default: a test run forces a flush and
so emits a burst of records seconds apart, while real flushes are one interval (default 600 s)
or more apart. Pass `--with-synthetic` to include them.
