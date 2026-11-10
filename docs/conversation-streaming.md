# Conversational execution and streaming

For the follow-up investigation using the installed Qwen 3.5 model, mixed
thinking/content/tool fields, historical Activity-only failures, and new live
acceptance tests, see [Ollama response repair](ollama-response-repair.md).

## Verified root causes

The response path spans Ollama, the bounded agent/controller, SQLite, SSE and the
frontend. Existing streaming/terminal guards in the working tree were inspected
and exercised; this repair extends them rather than adding another orchestrator.

The reproducible silent-chat defect was in reconciliation/rendering: the UI computed
live assistant messages from SSE but rendered the chat only when the separately
fetched `current.messages` list was nonempty. If session snapshot requests failed
while generation continued, the backend could persist and publish a valid answer
and reach Completed while the panel still showed its empty state. A deterministic
Playwright reproduction failed with **no assistant element**. Rendering from the
reconciled messages fixes that failure even while snapshots remain unavailable.

Other inspected gaps were repaired:

- Auto-scroll depended on `events.length`, which stops changing when the 2000-event
  ring is full. It now follows the latest event cursor, respecting upward scrolling.
- Delayed resync snapshots could replace newer live journal entries. Journal merge
  now preserves them. Delayed session selections cannot replace a newer selection.
- Draft ordering used content length. Authoritative content can shrink after
  filtering/correction; persisted message revisions now distinguish that from stale
  replay. Old progress cannot demote a persisted final answer.
- Valid tool-only adapter messages with `content: null` were treated as malformed.
  Null is normalized to empty tool-message content; invalid non-string content is
  still rejected. Text plus tool calls, indexed argument fragments and nonstreaming
  JSON adapters remain supported.
- Ollama token-limit termination could look like normal completion. `length` or
  `max_tokens` completion reasons now produce an explicit budget error/checkpoint
  while retaining partial output.
- Deterministic scan, screenshot and goal replies duplicated raw JSON instead of
  readable findings. They now use measured-result tables, literal image quotes,
  explicit uncertainty, candidate provenance and actionable checkpoint summaries.
  Full raw evidence remains in Activity/storage.

Historical backend causes addressed by the existing guarded path are also covered
by the current regression suite: nonstreaming-only model calls, drafts withheld
until completion review, activity-only errors, cancellation without a persisted
answer, and terminal events published before final message/state persistence.
The outer guard rejects a controller which returns success without a final answer.

## Conversation and bounded execution

Delegated model replies consume Ollama NDJSON incrementally; deterministic planners
retain evidence-backed summaries without an extra model call. Public text is saved
before its event is published. Separate `message.thinking` and split inline `<think>`
blocks are not displayed, persisted in conversation, or sent back as private traces.
Transport retry is bounded and only allowed before receiving output/actions. Errors,
empty replies, malformed responses, timeout, cancellation, restart and token-budget
termination retain partial text and produce a conversational blocker.

The existing goal controller, policy engine, scope checks, tool registry, per-run
budgets and cancellation remain authoritative. Goal runs continue past initial
scans; credentials unavailable to SSH do not disable permitted public discovery.
Milestone progress explains the intended action and actual observation. Completed
actions, hypotheses and checkpoints remain durable. `Continue task` authorizes one
new bounded goal run, not an unbounded background loop. Identical model-selected
actions are bounded, and a failed tool can lead to another permitted action.

Final replies preserve facts, evidence IDs, source paths/URLs, timestamps and
limitations. Public HTTP responses are not vulnerability proof, and retrieved flag
candidates do not prove challenge acceptance. No scope, auth, SSH, subprocess,
request-rate, timeout or tool-call policy was relaxed.

## Event contract

The journal has monotonic `event_id`, `run_id` and timestamps. Message identity is
stable across generation, finalization, reload and reconnect.

| Existing event name | Meaning |
| --- | --- |
| `user_message` | Persisted user message ID, text and attachment IDs; send/retry can render even if a snapshot fetch fails |
| `run_started` | Begins a bounded run |
| `assistant_status` | Thinking, running tools, generating or reviewing; no private reasoning |
| `assistant_message_started` | Stable persisted draft ID, initial revision and state |
| `answer_delta` | Draft ID, incremental text, authoritative cumulative content and monotonic revision; saved atomically before emission |
| `assistant_message` | Authoritative progress/final content and metadata; final promotes its draft in place |
| `tool_started`, `tool_finished`, `tool_error`, `tool_denied` | Existing policy-enforced tool execution, result/failure and raw evidence |
| `run_checkpoint`, `goal_checkpoint` | Saved plan/task-goal progress and continuation boundary |
| `run_finished`, `run_failed`, `run_cancelled` | Exactly one terminal event per initialized run, after the final answer/blocker and terminal run/session state are saved |
| `resync_required` | Cursor predates retained journal; merge a session snapshot without dropping newer live events |

SSE honors cursors/Last-Event-ID, sends heartbeat frames, and disables buffering and
caching. Browser parsing accepts LF/CRLF; text delivery is paced to animation frames
and flushed before control/terminal events. Readers are cancelled/released on
reconnection. Reconnecting never executes tools. New revision metadata is removed
from subsequent Ollama message history; legacy revision-less journals remain readable.

The existing terminal theme, sidebar and Activity panel remain. Central chat renders
sanitized Markdown, tables, code highlighting/copy controls, distinct roles,
collapsible progress, stream cursors and explicit execution states. Stop preserves
partial output; Retry creates a new bounded run. Following resumes when the reader
scrolls back to the bottom.

## Live Ollama investigation

At verification time, configured global default `qwen3.5:4b` was not installed. That
is an actionable model-selection issue, not evidence that all models are broken.
The most recent persisted session used installed
`huihui_ai/qwen3.5-abliterated:4b`, advertising completion, vision, tools and thinking;
its saved digest-specific tool and vision probes were positive. No user model
preference, active run, pairing credential or scope was modified for diagnostics.

A neutral text-only HTTP probe to that model returned:

- Nonstreaming: one completed response, nonempty assistant text, 8.85 s.
- Streaming: nine NDJSON frames, the same nonempty text, 0.25 s.
- Both: “BlackBox conversational response is working.” No private thinking text was
  exposed (the probe only counted hidden thinking characters).

The full production agent path was then tested with an isolated temporary app and
that exact model, copying only its saved capability probes by digest from the user
DB in read-only mode. No scope or private messages were copied. Command:

```bash
.venv/bin/python scripts/smoke_conversation.py --model huihui_ai/qwen3.5-abliterated:4b
```

Final live result: **Completed**, 16 streamed updates across the bounded control
loop, 4 model turns, 0 tool executions, 7.92 s. The final persisted sentence was
correct and only one terminal completion event was emitted. Artifact:
`qa-reports/conversation-live-ollama.json`. This is real local inference; security
tool scenarios use isolated deterministic fixtures, not public targets.

## Validation

The initial snapshot-failure browser reproduction failed **1/1** before the render
fix. The affected snapshot and authorized-inspection rerun passed **2/2** afterward.
All final deterministic/backend/browser checks are recorded below.

| Command | Actual result |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/unit/test_response_summaries.py tests/integration/test_conversation_streaming.py --junitxml=qa-reports/conversation-focused.xml` | **31 passed**, 0 failed, 0.85 s |
| `.venv/bin/python -m pytest -q --junitxml=qa-reports/conversation-full.xml` | **301 passed**, 0 failed, 0 skipped, 53.52 s |
| `npm --prefix frontend test` | **12 passed**, 0 failed |
| `npm --prefix frontend run typecheck` | Passed |
| `npm --prefix frontend run build` | Passed |
| `npm --prefix frontend run test:e2e` | **17 passed**, 0 failed, 16.4 s |
| Live-agent smoke command above | Passed against installed Ollama model |
| `git diff --check` | Passed |

Coverage includes normal replies, streamed text, private thinking, null tool text,
sequential tools then final answer, failure recovery, empty/malformed output, denial,
timeout retry/failure, token-budget truncation, cancellation, generic/goal checkpoints,
missing-final guards, persistence/reload, cursor replay, revisions, delayed snapshots,
user/assistant/tool event separation, actual Markdown/code rendering, scrolling and
partial output after Stop/reload. The browser authorized-lab test validates schema,
scope, mocked tool evidence and NDJSON rendering end to end.

## Files and practical limits

Extended `backend/blackbox/app.py`, `core/agent.py`, `core/goals.py`,
`core/ollama_client.py`; added `core/responses.py`. Extended frontend
`src/app.tsx`, `src/api.ts`, `src/conversation.ts`; existing Markdown renderer,
styles and Activity details were retained. Added/extended conversation backend,
response-summary, frontend reducer and Playwright tests. Added
`scripts/smoke_conversation.py` and updated this document/README. Existing QA token
optimizations and CLI lifecycle code were not changed.

No failing tests remain in the verified suites. The live check covers the named
4B model and a neutral conversation, not every installed model or a real external
room. Unsupported features, missing models and unavailable authorized access still
produce explicit limitations. Choose an installed model for new sessions; this work
did not download the absent configured default or silently change preferences.

The frontend has been rebuilt. Restart the backend and reload the page to apply
all event-contract changes to an already-running installation. A backend restart
creates new pairing credentials and revokes old network scopes as designed. The
actual developer backend was not restarted by this work.
