# Focused autonomous-goal regression run

Verified the request “Solve the authorized TryHackMe room at 127.0.0.1 and retrieve
flag.txt” without supplying SSH credentials. No external TryHackMe machine was
accessed. All targets were isolated loopback fixtures with explicit session scope.

## Commands and actual results

Commands ran in this order:

```bash
.venv/bin/python -m pytest -q tests/integration/test_goals.py --junitxml=qa-reports/goal-focused-mocked.xml
```

Initial deterministic discovery/SSH mocks plus loopback HTTP: **18 passed**,
0 failed, 0 skipped (3.08 s).

```bash
.venv/bin/python -m pytest -q tests/integration/test_goal_live.py --junitxml=qa-reports/goal-focused-live.xml
```

Initial real-tool integration: **1 failed** (9.25 s). Nmap reported an open port
as `unknown` despite receiving HTTP responses. The planner stopped with
`needs_access`, without attempting protocol verification. This was a real planner
coverage defect, not missing SSH credentials or a fabricated scan failure.

```bash
.venv/bin/python -m pytest -q tests/unit/test_scope.py tests/integration/test_planning.py::test_http_budget_configured_rate_timeout_and_failure tests/integration/test_planning.py::test_real_ffuf_rate_and_cache --junitxml=qa-reports/goal-focused-limits.xml
```

Authorization, scope, configured rate/runtime/request limits and actual ffuf pacing:
**26 passed**, 0 failed, 0 skipped (3.68 s).

After fixing protocol verification and adding its positive regression, reran the
first command: **19 passed**, 0 failed (3.14 s). Reran the live integration command:
**1 passed**, 0 failed (11.81 s).

Added a negative regression ensuring an unsuccessful protocol probe cannot invent
an HTTP endpoint, access or flag, then ran the final combined focused check:

```bash
.venv/bin/python -m pytest -q tests/integration/test_goals.py tests/integration/test_goal_live.py tests/unit/test_scope.py tests/integration/test_planning.py::test_http_budget_configured_rate_timeout_and_failure tests/integration/test_planning.py::test_real_ffuf_rate_and_cache --junitxml=qa-reports/goal-focused-final.xml
```

**47 passed, 0 failed, 0 skipped (18.58 s)**. These comprise 20 goal/routing cases,
one real-tool goal integration and 26 scope/limits cases. `git diff --check` passed.

## Fix and observed integration behavior

For a fresh complete executed scan reporting an open TCP listener with an unknown
service, the goal controller now selects a bounded HTTP HEAD probe before declaring
its public discovery plan exhausted. The action is explicitly a protocol hypothesis,
not an assertion that HTTP exists. Only the actual HTTP response supplies HTTP
endpoint evidence. Scope, port, timeout, request-budget and registry validation
remain mandatory. Failed probes remain failed observations, never verified services.
Ports 443/8443 use certificate-verifying HTTPS; other unknown ports test HTTP.

The real integration used installed Nmap, curl and ffuf, production adapters and
an ephemeral HTTP service. SSH agent access was removed and no SSH profile existed.
Ollama metadata was mocked; goal planning made zero chat/model inference calls.
A random synthetic flag was independently planted in the fixture and checked
against the actual final tool output.

Observed sequence:

1. Nmap scanned only the attested loopback host and fixture port, with service
   detection, `--max-rate 20`, `--host-timeout 30s` and one retry.
2. Bounded curl HEAD verified HTTP on the unknown service.
3. HTTP inspection of `/` and `/flag.txt` observed a 404 for the direct flag path.
4. Real ffuf discovered `/docs`, using 12 curated paths, rate 5/s, concurrency 1,
   max runtime 40 s and request timeout 5 s; redirects were not followed.
5. HTTP inspection of `/docs/flag.txt` retrieved the independently planted candidate.
6. The persistent goal stored the exact tool ID, URL, observation timestamp and
   actual candidate content. It did not claim independent room acceptance.

Actual usage: **6 tool calls, 16 budgeted HTTP-adapter requests, 0 model turns**.
The fixture logged 18 HTTP requests including two Nmap service-detection probes;
Nmap probes are governed by the scanner's separate execution limits. The explicit
HTTP request budget was 32. No SSH action, credential request, vulnerability claim
or exploitation occurred.

## Requirement checks

| Requirement | Evidence |
| --- | --- |
| Goal rather than immediate SSH | Persistent decomposition; seeded premature SSH model reply never used |
| Existing evidence and missing prerequisites | Evidence reuse skips Nmap; missing protocol causes a bounded verification step |
| Permitted service and HTTP discovery | Real Nmap → HEAD → HTTP → ffuf → candidate read |
| Observation-driven continuation | Direct flag 404 triggers content discovery; SSH access failure remains tool-local |
| No fabricated results | Soft-404, failed scan and failed protocol cases produce no result; real candidate equals planted bytes |
| Authorization and limits | Scope/attestation, target/port validation, DNS rebinding, actual ffuf pacing and timeout/budget tests pass |
| Persistent checkpoint | Tool/request/step/wall-time limits, explicit continuation, cancellation and restart tests pass |

## Artifacts, changed files and remaining limits

JUnit reports and the actual command/event/evidence trace are in `qa-reports/`:
`goal-focused-final.xml`, `goal-focused-live.xml`, `goal-live-trace.json`.
The initial failure was preserved as `initial-goal-focused-live.xml` and
`initial-goal-live-trace.json`.

Changed `backend/blackbox/core/goals.py`, extended
`tests/integration/test_goals.py`, added `tests/integration/test_goal_live.py` and
this report. QA orchestration/token optimization code was not changed.

No remaining failures were detected in these 47 focused cases. This does not verify
an external room, real-model reasoning, unsupported protocols, arbitrary TLS ports
or exploitation-dependent flag retrieval. HTTP candidate recognition and existing
TLS/access policies still apply. No external target configuration or credentials
were used. A full frontend or whole-repository rerun was unnecessary for this
backend-only fix; the affected goal/planner/HTTP safety cases were rerun instead.
