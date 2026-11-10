# Agent intelligence and orchestration implementation

Implemented on 2026-11-09, extending the existing PRD architecture, registry,
executor, scope policy, SQLite database and BlackTurq Activity UI.

## Delivered behavior

- Current images route to a metadata-supported vision model, with an explicit
  installed fallback for text-only chat models. A guessed positive probe cannot
  promote metadata that does not advertise vision. Strict observations preserve
  visible text, quoted extracted fields, uncertainty and inferred service-to-URL
  mappings. Correlations expose agreement/conflict with stored executed scans.
- Deterministic typed plans classify intent, retrieve scoped evidence, resolve the
  conversation target/protocol and validate tool schemas. Common tasks need no
  planning/answer LLM call. Complex tasks retain the existing model tool loop.
- HTTP discovery selects ffuf, headers prefer curl, technologies select WhatWeb,
  TLS selects OpenSSL, and scan analysis uses the internal parser/repository.
  SSH is separately gated by remote-access intent and remains read-only SFTP.
- Structured scan records retain arguments, target, timestamps, source, status,
  completeness and attributable findings. Parsers handle Nmap XML, ffuf JSON/JSON
  lines and terminal output. Older raw journals import lazily with unknown original
  scope binding and completeness; imported data cannot become an executed cache hit.
- Equivalent complete successful executed scans are reused within the configured
  TTL and identical authorization scope/session. Stale results, newer failed or
  incomplete equivalents and explicit rescan prevent hits. Multiple candidate URLs
  require a specific URL. Every reuse still passes policy/destination validation.
- HTTP discovery reserves its wordlist request budget, enforces configured rate,
  concurrency and runtime, preserves response codes and rejects incomplete output
  as success. General model loops also cannot finish successfully when all requested
  network/access tools failed. Full evidence remains stored; model/planning context
  is bounded independently of raw outputs.

## Controlled demonstration

```bash
.venv/bin/python scripts/demo_orchestration.py --output qa-reports/orchestration-demo
```

The script creates an isolated temporary application/database and loopback HTTP
server. It generates a screenshot of an Nmap terminal, mocks its visual transcription
and routes it from a text-only chat model to the configured vision fixture. The
actual ffuf binary executes HTTP discovery; neither Nmap nor SSH runs.

The resulting `qa-reports/orchestration-demo/report.md`, `screenshot.png` and
`trace.json` contain the actual plan, model-routing trace, argv, request observations,
stored records, final response and reuse event. The demo checks that:

1. Only the visibly named HTTP service supplies the nonstandard endpoint.
2. Screenshot time is unknown and its transcription is explicitly unverified.
3. ffuf executes 12 requests, maximum 5 requests/second, concurrency 1, timeout
   40 seconds, budget 24. Responses include HTTP 200, 403 and 404.
4. Separate screenshot and successful HTTP records are saved.
5. Repeating “Fuzz the web server” adds no HTTP requests or vision calls.

## Verification

| Command | Actual result |
| --- | --- |
| `.venv/bin/python -m pytest -q` | **195 passed**, 0 failed, 0 skipped; 15.51 seconds |
| `npm --prefix frontend run typecheck` | Passed |
| `npm --prefix frontend run build` | Passed (TypeScript and Vite production build) |
| `npm --prefix frontend run test:e2e` | **13 passed**, 0 failed; 9.9 seconds |
| `.venv/bin/python scripts/demo_orchestration.py --output qa-reports/orchestration-demo` | Passed; real ffuf, 12 loopback requests, 200/403/404, cached repeat adds zero requests |
| `git diff --check` | Passed |

Added 45 backend regression cases and one browser case. Coverage includes every
requested acceptance scenario: literal Nmap screenshot extraction, non-vision model
rejection/fallback and guessed-probe rejection, HTTP-vs-SSH routing, address-only
SSH rejection, fresh/stale/failed/incomplete result policy, explicit rescans,
nonstandard ports, scope/session isolation, failures without fabricated success,
request/rate/runtime limits, legacy/import parsing, evidence conflicts and bounded
context. Browser tests preserve the existing theme/layout checks and verify that
structured observations and plans are visible.

The final focused integration run also passed after adding explicit import freshness to returned evidence.

No new dependencies were needed. The installed binaries used for the demo/tests
include ffuf 2.1.0 and Nmap 7.992. Tests use isolated local fixtures, not public
targets. Existing uncommitted protocol/QA regression tests were included in the full
backend run.

## Limits

- Real-model OCR/visual accuracy was not benchmarked. Automated vision tests and
  the demo use a deterministic Ollama transport; real ffuf, curl, Nmap, ping and
  local HTTP/SFTP fixtures run in integration tests. Vision transcription remains
  fallible and is never presented as independent verification.
- Screenshot freshness uses upload/observation time when the original scan time
  is not visible. Candidate URLs inferred from service labels are labelled as
  assumptions until the executor measures HTTP responses.
- HTTPS discovery keeps the existing TLS-verifying native HTTP adapter because
  ffuf skips certificate verification. WhatWeb is restricted to pinned HTTP;
  OpenSSL checks handshake/certificate validation, not every TLS cipher/configuration.
- SQL injection intent is recognized but sqlmap remains outside the current PRD
  policy. Arbitrary remote commands are likewise unavailable; SSH means existing
  authorized read-only SFTP access. No unrestricted shell was added.
- Deterministic routing covers the common task patterns tested here; complex or
  unfamiliar phrasing uses the existing model loop. Ambiguous endpoints require a
  specific target instead of guessing. Scan retention is capped at 1000/session.

## Files changed for this task

New modules/scripts/tests:

- `backend/blackbox/core/planner.py`
- `backend/blackbox/core/scans.py`
- `backend/blackbox/core/vision.py`
- `backend/blackbox/tools/scans.py`
- `scripts/demo_orchestration.py`
- `tests/unit/test_intent_parsers.py`
- `tests/integration/test_planning.py`
- `docs/orchestration-report.md`

Existing modules extended:

- `backend/blackbox/app.py`
- `backend/blackbox/core/agent.py`
- `backend/blackbox/core/ollama_client.py`
- `backend/blackbox/core/policy.py`
- `backend/blackbox/settings.py`
- `backend/blackbox/storage/db.py`
- `backend/blackbox/tools/base.py`
- `backend/blackbox/tools/network.py`
- `backend/blackbox/tools/registry.py`
- `backend/blackbox/tools/runner.py`
- `backend/blackbox/tools/web.py`
- `frontend/src/app.tsx`
- `frontend/src/components/Activity.tsx`
- `frontend/src/components/Dialogs.tsx`
- `tests/conftest.py`
- `tests/integration/test_api.py`
- `tests/integration/test_tools.py`
- `tests/e2e/server.py`
- `tests/e2e/paste-image.spec.ts`
- `README.md`, `config.example.yaml`, `docs/architecture.md`, `docs/security.md`,
  `docs/tools.md`

Pre-existing uncommitted agent/QA changes were preserved. The pre-existing
`tests/integration/test_protocol.py`, `scripts/qa_validate_shutdown.py`, prior QA
report and README bundle were not replaced or counted as task additions.
