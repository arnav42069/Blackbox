# Tool argument validation and recovery

The observed failure was not caused by absent tool schemas: every registry tool
already used a Pydantic argument model. Workspace path constraints, however, were
enforced late inside the adapter, and the planner received an undifferentiated
failure string. The general agent repeated a failed call and raised its
`failed twice without new evidence` stop condition before considering a different
valid action.

## Lifecycle

1. Ollama receives strict registered JSON schemas and schema-validated syntax
   examples. Examples are explicitly not observations of available files, targets
   or access. `workspace.read.path` is required and documented as a **local
   application-workspace relative file**, not a remote machine's `/home` path.
2. The streaming client assembles tool calls; `parse_calls` accepts an object or
   JSON-encoded argument object and bounds call count/size. Malformed envelopes
   retain the existing one-repair limit.
3. The registry records requested arguments, performs narrow normalization, and
   validates the strict typed model. Unknown fields remain forbidden. Normalized
   arguments do not imply permission: policy, target, scope, limits and manual
   approval are checked independently.
4. A canonical per-run failure key prevents re-executing an identical failed call.
   Default values and equivalent URL/scan syntax do not create a new operation;
   toggling `rescan` cannot evade this guard. Replays have their own audit record
   and `tool_retry_suppressed` event referring to the original evidence, but no
   `tool_started` event or adapter execution.
5. Actual outcomes return `ToolResult.error`, a typed category, stable code,
   field-specific messages, actionable suggestions and optional prior evidence ID.
   Invalid input is never labelled as validated arguments. Full requested/validated
   argument provenance is kept without leaking Pydantic raw input values.
6. The model receives the actual result plus controller recovery guidance. It can
   inspect workspace entries, select an actual relative path, inspect existing
   endpoint evidence, or choose another permitted tool. A second failed proposal
   no longer immediately aborts before a different action can be considered.
   Persistent identical proposals still hit the existing bounded duplicate/turn
   guard and produce a saved, visible failure rather than an infinite loop.
7. Final answers retain actual outcome/evidence summaries and existing
   persistence-before-terminal ordering. Activity shows collapsible structured
   recovery guidance; no failure is reported as successful execution.

## Safe repairs and filesystem boundary

Accepted deterministic repairs are `./notes.txt` → `notes.txt`, an absolute path
**lexically inside the exact configured workspace** → its relative equivalent,
decimal-string integer fields → bounded integers, exact `true`/`false` boolean
strings → booleans, and supported HTTP method casing. All still pass schema and
policy validation. No scheme, credentials, host, filename or basename is guessed.

The workspace remains `<configured data_dir>/workspace`; no extra project/home
roots were authorized. External absolute paths, traversal (including apparently
collapsible `docs/../file`), URL paths, home expansion, secret stores and symlink
escapes remain rejected. A missing file is a permanent resource error, not proof
of remote access or a reason to guess another absolute filename.

Workspace reads use descriptor-relative traversal with `O_NOFOLLOW`; each opened
directory is pinned before opening the next component. File type is verified with
`fstat`, and nonblocking opens prevent special-file hangs. A symlink swapped in
between path checking and opening cannot redirect a read outside the workspace.
Listing uses a pinned directory descriptor; search reads use the same confined
open helper. Internal resolved aliases may be read only when still confined.

## Error categories

| Kind | Examples | Recovery |
| --- | --- | --- |
| validation | Missing/unknown fields, wrong types, invalid local path or URL | Correct named fields; use list/search and registered constraints |
| policy_denial | Out-of-scope target, secret store or symlink escape | No automatic retry or permission expansion; explain denial |
| transient | Connection reset/refused, timeout, selected curl transport exits | Inspect evidence or use another known permitted endpoint; a later explicit run may retry |
| permanent | Missing/nonregular file, filesystem access failure, nontransient executor failure | Find an existing resource or state the blocker |
| unavailable | Missing executable, unavailable configured access | Choose an available registered adapter or explain the missing dependency/access |

`retryable` is diagnostic, not authority for blind same-run retries. Budgets count
failed/suppressed proposals too. No unrestricted commands, SSH substitutions for
HTTP failures, scope bypasses or reduced TLS verification were introduced.

## Verification

```bash
.venv/bin/python -m pytest -q tests/integration/test_tool_recovery.py \
  tests/integration/test_conversation_streaming.py tests/integration/test_protocol.py \
  --junitxml=qa-reports/tool-recovery-focused.xml
.venv/bin/python -m pytest -q tests/integration/test_tool_recovery.py \
  tests/integration/test_planning.py --junitxml=qa-reports/tool-recovery-planning.xml
.venv/bin/python -m pytest -q --junitxml=qa-reports/tool-recovery-full.xml
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run test:e2e
git diff --check
```

Regressions exercise every registered schema/example, safe repairs, traversal and
unauthorized absolute paths, symlink escapes and a deliberate validation/open
race, repeated bad workspace calls followed by list/read recovery, suppressed
repeated HTTP failures followed by a different authorized endpoint, unavailable
tools, permanent missing files, nonobject inputs, and bounded noncooperative-model
failure. All network failures are mocked or use isolated local fixtures.

The browser regression submits the recovery request through the normal UI, sees
validation and replay-suppression details, receives real local file contents, and
reloads the persisted answer. Final validation counts are recorded below.

Intermediate testing found and fixed URL/home-prefix paths being accepted as
literal filenames, a recovery fixture being accidentally routed to scan analysis
by its wording, and nullable `scan.lookup.target` breaking canonical failure keys.
The old combined fuzzing-timeout/failure test intentionally replayed a timed-out
call; it was split into independent contexts so both failures remain tested
without bypassing replay suppression.

## Files and limitations

Backend changes: `tools/base.py`, new `tools/validation.py`, `tools/registry.py`,
`tools/workspace.py`, `tools/web.py`, `core/agent.py`, `core/scans.py`.
UI: `frontend/src/components/Activity.tsx`. Tests: new
`tests/integration/test_tool_recovery.py`, extended `test_planning.py`,
`tests/e2e/server.py` and `tests/e2e/chat-tool-loop.spec.ts`. Documentation:
this file and `README.md`.

Existing cached results remain readable because new result/error fields have
defaults. No QA-orchestrator or authorization rules were replaced. A model which
keeps ignoring errors can still exhaust the bounded recovery loop; BlackBox then
reports an explicit failure. Recovery does not invent missing files or access.
No real external target, actual developer backend, Codex configuration or prompt
queue was modified by these implementation tests. The frontend is rebuilt;
restart/reload is required for an already-running backend to load the Python change.
