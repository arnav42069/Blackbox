# Ollama response lifecycle investigation

## Findings

The installed `huihui_ai/qwen3.5-abliterated:4b` works through Ollama's HTTP chat
API as well as its CLI. A baseline run through the current isolated BlackBox
agent completed in 14.79 seconds with 8 public deltas and a persisted answer.
This was not reproduced as a general model failure.

Read-only inspection of the normal BlackBox database showed the older response
contract: tool records, `assistant_progress`, `run_failed`, and a few unversioned
`answer_delta` events, but no `assistant_message` or `assistant_message_started`
events. The latest stored failed run recorded five tool calls and ended on
repeated HTTP-tool failure without a final conversation message. Another run
exhausted twelve model turns after a host check. The older agent's exception
path emitted Activity errors and saved terminal state without saving a final
assistant explanation. Successful Ollama CLI inference does not exercise that path.

No normal-data backend was running during this investigation, so its former
in-memory code/version cannot be inspected directly. The stored event format and
the old code path explain the observed Activity-only failures; they do not prove
that every previously reported empty panel had the same cause. The current tree
already contained streaming, final-answer guards, and snapshot/rendering repairs;
these were retained and exercised rather than replaced.

Two additional defects were reproduced against the current tree before changes:

1. With `thinking`, `content`, and `tool_calls` together, the tool-call record
   persisted empty text even though in-memory history had the public content.
   Reconstructing history after reload lost that text.
2. A completed response with separate thinking but no public content immediately
   failed without one bounded final-answer attempt.

The new two-test reproduction initially failed **2/2**. Separate private thinking
is deliberately not treated as assistant content.

## Repair

- Preserve public text on structured assistant tool-call records. The renderer
  excludes those records from duplicate bubbles; streamed milestone text and final
  responses remain visible. Reconstructed model history retains the text and calls.
- Count public chunks and private thinking characters for diagnosis without
  persisting or publishing the thinking itself. `model_response` events include
  counts and stream/review mode, never private trace text.
- Give a reasoning-only completion one extra final-answer request, without tools,
  within the existing turn/time limits. A second empty reply produces a persisted,
  visible failure rather than a retry loop. Truly empty replies still fail visibly.
- Keep the existing tool-result-to-next-model-turn path, completion review,
  persistence-before-terminal ordering, partial-output retention, and cancellation.
- Add a frontend terminal-event safeguard: missing final output renders an explicit
  blocker, including when replaying an older journal. A genuine final message
  replaces the safeguard without duplication.
- Advertise `conversation_protocol: 1` in preflight. A newer UI connected to an
  older backend shows a visible restart/pairing instruction instead of concealing
  the protocol mismatch in Activity or dependency details.

The first live inspection exposed another concrete defect: the model invented a
date and HTTP latency, and its completion review accepted them. Tool observations
now include the literal measured UTC timestamp and explicitly state that HTTP
latency was not measured. The response path removes unsupported timing metadata
from streamed drafts and final text, and appends controller-owned timestamps with
actual tool outcomes. Regression coverage includes prose such as “HTTP 200 was
observed at http://…” so legitimate findings are retained. This narrow metadata
check is not a general semantic proof of every statement a model can generate.

No scope, authorization, tool schema, subprocess security, rate limit, execution
budget, cancellation policy, or QA-orchestrator token optimization was weakened.

## Actual installed-model integration

```bash
BLACKBOX_LIVE_OLLAMA_MODEL=huihui_ai/qwen3.5-abliterated:4b \
  .venv/bin/python -m pytest -q -s tests/integration/test_ollama_live_conversation.py \
  --junitxml=qa-reports/ollama-live.xml
```

Final result: **2 passed in 55.16 seconds**. Model digest:
`9acbd90c2645120ea1e4a5fe7ec9c4fe000e547ccebc8a24f950315d582003e6`.
Thinking was enabled to exercise separate thinking/content frames.

| Request | Actual result |
| --- | --- |
| `Hello, what can you do?` | Completed, 425 public streamed updates, no tools, 26.12 s |
| Authorized loopback HTTP inspection | Completed, one real scoped `web.inspect` GET, measured HTTP 200 and fixture title, 307 public updates, 28.87 s |

Both tests exercise the production authenticated message API, actual installed
Ollama, production SSE generator during execution, SQLite persistence, terminal
ordering, and session reload. They assert exactly one successful terminal event,
nonempty final text, and evidence IDs. The inspection verifies the exact recorded
UTC timestamp and rejects invented elapsed-time fields. The local fixture is
ephemeral; no public target, user lab, remote SSH credential, or user scope is used.
Only digest-matched saved capability probes are copied from the normal database
read-only; no user messages or authorizations are copied. A missing model/service
or unverified tool capability fails an explicitly opted-in live test.

Artifacts: `qa-reports/ollama-live-greeting.json`,
`qa-reports/ollama-live-inspection.json`, and `qa-reports/ollama-live.xml`.
These contain public answers and diagnostic counts, not private thinking text.
Browser rendering is tested separately with deterministic scoped tool/model
fixtures, avoiding a nondeterministic model in the default UI suite.

## Regression commands and results

```bash
.venv/bin/python -m pytest -q tests/integration/test_protocol.py \
  tests/integration/test_conversation_streaming.py tests/unit/test_response_summaries.py \
  --junitxml=qa-reports/ollama-conversation-focused.xml
.venv/bin/python -m pytest -q --junitxml=qa-reports/ollama-conversation-full.xml
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run test:e2e
git diff --check
```

Focused backend: **43 passed**. Frontend unit tests: **14 passed**. Production build
and TypeScript checks passed. Browser suite: **18 passed in 19.7 seconds**.
The default backend suite leaves the two real-model tests opt-in; the command above
runs them explicitly against the installed model.

Intermediate failures were investigated and fixed: new diagnostic counting initially
bypassed malformed-review retry for string/integer messages (two protocol tests);
the timing filter initially removed a valid “observed at URL” paragraph; the old
backend warning initially lived inside collapsed dependency details; and an offline
browser fixture needed the current protocol marker to model an up-to-date backend.
The affected regression suite and full browser suite were rerun after fixes.

Final full backend rerun: **318 passed, 2 skipped in 69.95 seconds**. The two
skips are the opt-in installed-model tests, which separately passed **2/2** as
recorded above. No failures remain in the final backend, browser, frontend-unit,
build/typecheck, or live-model runs. `git diff --check` passed.

## Files and applying the repair

Backend changes: `backend/blackbox/app.py`, `core/ollama_client.py`, `core/agent.py`,
`core/responses.py`. Frontend: `frontend/src/app.tsx`, `conversation.ts`,
`components/Messages.tsx`. Tests: `tests/integration/test_conversation_streaming.py`,
new `test_ollama_live_conversation.py`, `tests/unit/test_response_summaries.py`,
`tests/frontend/conversation.test.ts`, `tests/e2e/chat-tool-loop.spec.ts`.
Documentation: `README.md`, `docs/conversation-streaming.md`, and this report.

The frontend has been rebuilt. The installed launcher uses this checkout's editable
environment, so no Python reinstall is needed. Start BlackBox with the installed
model, or restart an existing backend and reload its new pairing URL:

```bash
blackbox restart --model huihui_ai/qwen3.5-abliterated:4b
```

Restart revokes old scope attestations and pairing credentials as designed. No
developer backend was stopped or restarted by these tests. Existing historical
failed runs are not rewritten to claim success; their terminal journals now produce
a visible explanation in the UI.
