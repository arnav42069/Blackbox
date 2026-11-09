# Persistent QA workflow

The Python controller `scripts/qa_loop.py` reuses pytest, Node Playwright, system Chromium, and existing deterministic Ollama/HTTP/SFTP fixtures. No dependencies or models are downloaded. It never opens production BlackBox data, pairing tokens or SSH profiles. The isolated browser app pairs through the normal `/api/pair` endpoint.

```bash
cd ~/Projects/blackbox
./scripts/qa-loop.sh --report-only
./scripts/qa-loop.sh --demo --once --budget-seconds 600
./scripts/qa-loop.sh --once
./scripts/qa-loop.sh --max-cycles 5
./scripts/qa-loop.sh --stop
```

`--report-only` executes all tests without Codex or Git mutations. `--once` allows one worker invocation followed by focused, subsystem and full regression checks. At most five worker invocations are allowed across all groups (`--max-cycles`); each failure group gets at most two attempts. Ctrl+C, SIGTERM and `--stop` stop owned child processes and preserve partial reports.

The default `--test-port auto` prefers port 8765 when free, otherwise selects an available loopback port. An explicit port, such as `--test-port 8876`, never falls back silently. An occupied explicit port is an infrastructure failure; no existing server is killed, paired or reused. The selected URL appears in `report.json` as `server_url`.

Exit codes distinguish outcomes:

| Code | Meaning |
|---|---|
| 0 | All executed checks passed |
| 1 | Test/infrastructure failures or an exhausted resource/repair bound |
| 2 | Intervention required or a harness error |
| 130 | Intentional Ctrl+C, SIGTERM or `--stop` shutdown |

Every final JSON report contains `exit_code`, `status`, `stop_reason`, `stop_request`, per-command exit codes, and failure categories. A negative command code such as -15 means the child received SIGTERM; it does not mean the application failed its tests.

## Source isolation and ownership

Report-only mode freezes reviewed source into `.runtime/qa-runs/<run-id>/`, records hashes/modes, links installed dependencies, and builds/tests there. Private runtime/config/session data, `.env`, caches and existing build output are excluded. Source hashes are checked before and after copying and before claiming a passing current-source result. Concurrent development cannot overwrite this run's fixture/build; source drift produces intervention rather than a misleading pass. Report-only mode therefore does not need to treat an idle interactive Codex process as an active source writer.

The demo also starts from a frozen source copy with independent Git metadata. It checkpoints a `qa/repair-<run-id>` branch/worktree, injects the metrics regression there, and never applies changes to the development tree. The real worker is checked for competing processes in its own isolated worktree.

Ordinary repair mode can apply verified fixes to the development checkout. It holds `.runtime/qa-writer.lock`, refuses other interactive Codex sessions attached to the checkout, and checks again while the worker runs and before application. Close other editing sessions before running ordinary repairs. Use the shared lease for future interactive sessions:

```bash
./scripts/qa-edit.sh codex
./scripts/qa-edit.sh your-editor
```

Idle app-server transports and the invoking coordinator are excluded. The coordinator must wait while the worker edits. All writers must cooperate with the advisory lease; an arbitrary editor cannot be physically prevented from writing. Original source/index fingerprints are checked before applying any fix, and unrelated changes cause intervention and are preserved.

The checkout originally had no `.git`. Integration created an initial source commit before applying the QA files. Ordinary repair mode requires an existing commit. A temporary Git index checkpoints tracked and reviewed untracked source without changing the user's index or branch. No reset, stash, checkout or history rewriting is used. Worktrees and evidence remain reviewable under `.runtime/` and `qa-reports/`.

## Checks and evidence

The focused-first workflow keeps broad QA output out of the worker context:

1. Run one complete baseline: Python unit/API/integration/QA tests, frontend typecheck/build, Playwright, and optional real-model probes. Collect structured failures from JUnit and Playwright JSON.
2. Group failures by Python test file, browser spec file, or frontend compiler/build checks. Preserve exact pytest node IDs, including classes and parameters, through the opt-in JUnit plugin. Browser IDs/selectors use file/line, escaped title and project; focused browser commands rebuild the UI first because the fixture serves `dist/`. A failed prerequisite build prevents stale browser output from being tested. Compiler/build failures reproduce through the failing npm command.
3. Reproduce each group with those focused commands before invoking Codex. An unreproduced failure, changed failure set, missing results or an infrastructure/scope/model failure requires intervention. A preceding repair may resolve a later group; that group still gets a focused check.
4. Send a concise report for that group: sanitized diagnostic tails (up to 1,800 characters each and 12,000 characters in total), exact targets, and at most twelve relevant source paths inferred from tracebacks, compiler diagnostics and imports used by the failing test. Preload the exact failing test functions and small source files in at most 6,000 characters of excerpts to avoid repeated worker reads. Source hints guide inspection, not edit permissions. The prompt is capped at 24,000 characters; oversized groups require review. Full QA logs and unrelated history remain controller evidence. The worker uses supplied excerpts, batches any extra reads and returns structured replacements (`path`, exact `old`, `new`) using `--output-schema`. It does not edit files or run tests/Git checks. This lets a simple repair finish in one model response without an edit-tool round trip.
5. Check measured usage and validate the entire proposal before applying replacements in the isolated worktree. Allow at most twenty replacements within the existing patch budget; every old block must match exactly once. Reject protected/noncanonical paths, missing files, symlinks, ambiguous matches and malformed proposals before writing any source. Validate the resulting diff and Git state before running the exact failing targets again. Missing/skipped targets cannot establish a fix. Only after they pass, run affected subsystem checks: the Python test directory for a harness-only change; all Python unit/integration/QA tests for backend changes because app fixtures and tool dispatch cross directories; typecheck/build and all browser tests for frontend changes. Known failures from other baseline groups remain pending; new failures stop acceptance.
6. A persistent original failure gets one retry with High reasoning, within the overall invocation/time/token limits. New failures, protected edits and environment failures stop immediately. After every group passes focused/subsystem checks, run the complete regression suite exactly once, including requested real-model probes. Any failure rejects the repair; no additional worker runs follow that gate.

Backend and browser fixtures are recreated for each command. Reports label stages `baseline`, `reproduce`, `focused`, `subsystem` and `regression`; each stage retains its commands, selected targets, results and duration. The baseline is the only full run when no repair is needed.

Fixed oracles check completion promises, missing tool calls, executor/journal disagreement, freshly planted workspace observations, SSH flag retrieval, scope/redirect denial, cancellation and descendant cleanup. Host tests compare production executor fields with actual OS commands. The model does not grade the QA result. Defined fixture contracts and measured fields establish ground truth; arbitrary model prose is not proof of execution.

Real local inference is opt-in:

```bash
./scripts/qa-loop.sh --report-only --model huihui_ai/qwen3.5-abliterated:4b --budget-seconds 300
```

This checks an independently colored image, application image/tool probes and a real `host.preflight` run checked against `os.uname()`. Capability and agent-completion results are reported separately: successful tool calls do not excuse invented extra work or a denied terminal run. No target scope is granted to the real-model session. Unsupported behavior and offline Ollama fail explicitly. Without `--model`, real inference is listed as untested; deterministic protocol fixtures remain fully exercised.

`qa-reports/<UTC-run-id>/` contains JSON/Markdown reports, JUnit and Playwright results, desktop/tablet/phone screenshots, console/page/network errors, API responses, tool journals, seed/fix patches, and worker event metadata/exit codes. `qa-reports/latest.json` points to the latest run. The directory is private and Git-ignored. Pairing responses, headers, image bytes and browser traces/HAR are excluded. Text evidence is redacted. Worker reasoning and arbitrary command stdout are not retained; event types, command/file-change statuses, usage, error summaries and exit codes are retained. All app evidence is from synthetic sessions and bounded host checks.

## Repair boundaries and budgets

The real worker uses normal saved Codex authentication and:

```text
codex exec --ignore-user-config --sandbox workspace-write
  --config approval_policy="never"
  --config sandbox_workspace_write.network_access=false
  --config model_reasoning_effort="medium"
  --config features.multi_agent=false --output-schema REPORT/repair-schema.json
  --json --ephemeral -C WORKTREE -
```

There are no approval/sandbox bypass flags, root commands or unrestricted shell tools. Approval-requiring actions fail. Ignoring user config prevents it from widening permissions or injecting hooks/MCP servers. Workspace-write is a write boundary, not complete read isolation for the local OS user; arbitrary worker stdout is therefore not retained.

Tests, dependencies, prompts, QA code, auth/scope/policy/budget/tool executors and security-sensitive UI/API files are protected. Completion controls, image validation, retention, CLI setup, sanitized message rendering, approval UI and the main authentication UI are also protected. The demo allows only `scripts/qa_metrics.py`. Deletions, mode changes, symlinks, unknown files, worker changes to Git HEAD/index and oversized patches require intervention. Checks run before retests and application. Infrastructure, model and scope failures require intervention. BlackBox's safety restrictions are never weakened. Tests use localhost fixtures only; external labs require separate explicit authorization and reviewed scope configuration.

After focused/full checks pass, a non-demo patch uses `git apply --check` then `git apply`, only if original source/index fingerprints are unchanged. The user's index is preserved. Failed/interrupted repairs stay isolated. Demo patches are retained as evidence and never applied to development source.

Default budgets: 900 seconds total, 180 seconds per worker/probe, 1 MiB captured output per command, 64 MiB report storage, and **30,000 aggregate worker input + output tokens**. Configure tokens with `--max-worker-tokens 30000` and initial reasoning with `--worker-reasoning medium|high` (default Medium; second attempt always High). The prompt includes the remaining budget. Disk/output limits are polled, allowing a small bounded overshoot. Actual token usage is available at turn completion and stops subsequent work; this is not a provider-side hard token or monetary cap. A single turn can exceed the budget; such a repair is rejected. Missing usage is recorded as unavailable and prevents acceptance, never reported as a zero-cost repair.

`report.json` records aggregate `worker_tokens`, `worker_usage_complete`, per-command input/cached-input/output/total usage, per-attempt reasoning, remaining budget, prompt bytes, worker duration, exit code, changed files, validation stages and acceptance/rejection. Cached input is already included in input tokens and is not counted twice. Total workflow duration and command durations are recorded separately. Interrupted workers may have incomplete usage; a partial total must not be presented as complete. Worker event logs retain usage metadata while discarding model reasoning and command stdout.

## Repeatable repair and shutdown checks

```bash
./scripts/qa-loop.sh --demo --once --budget-seconds 600
PYTHONPATH=. .venv/bin/python scripts/qa_validate_shutdown.py
```

The demo seeds one real bug: skipped results are counted as failures. The unchanged `test_failure_metrics_exclude_skipped_results` must fail; controller regression tests may also detect the same shared metrics bug. A real `codex exec` worker may propose replacements only in `scripts/qa_metrics.py`, then the focused test, QA subsystem and complete suite run again. Seed/fix patches and worker exit/usage evidence are retained. The development tree is never seeded or patched.

The historical demo used **81,952 worker tokens**, recorded in `qa-reports/final-qa-report.json` (demo run `20261109T102953Z-63f9fe`). Compare a new demo's measured `worker_tokens` only when `worker_usage_complete` is true. Report the absolute difference and percentage against 81,952, together with run ID, durations and outcomes. This is a single controlled metrics repair, not a projected saving for arbitrary defects or a measurement of coordinator tokens, elapsed-time improvement or cost. Do not infer savings from the 30,000-token setting alone.

### Measured controlled repair — 2026-11-09

The final `--demo --once --budget-seconds 600` run, `20261109T124700Z-b49172`, passed with the default Medium reasoning and 30,000-token budget. A single schema response proposed the exact metrics replacement; there were no worker tool calls. It used **16,078 input + 67 output = 16,145 tokens**, with zero cached input, in **6.23 worker seconds**. The complete workflow took **81.86 seconds**. Compared with the historical 81,952-token worker run, the successful cycle used **65,807 fewer tokens (80.3%)**. The historical provider default was not pinned, and the regression suite has grown; this is an observed comparison for the same seeded bug, not a matched multi-run benchmark.

The seeded bug triggered two assertions in separate test files. The first exact reproduction failed, one replacement fixed it, its focused rerun and all 57 QA subsystem tests passed, and the second group's exact reproduction passed without another worker. The single complete acceptance regression then passed **148 Python tests, 12 browser tests, typecheck and build**, with zero failures/skips (162 result entries). Only `scripts/qa_metrics.py` changed in the demo worktree; the development tree and all oracles were untouched.

Two preliminary measurements exposed the cost of worker tool round trips and were rejected at the default token gate:

| Run | Approach | Measured worker tokens | Worker seconds | Workflow seconds | Result |
|---|---|---:|---:|---:|---|
| `20261109T122400Z-3c6a3f` | Concise report, worker reads and edits | 52,550 | 14.06 | 46.18 | Over budget; no acceptance retests |
| `20261109T124416Z-e51753` | Preloaded excerpts, worker edit tool | 36,366 | 10.46 | 43.06 | Over budget; no acceptance retests |
| `20261109T124700Z-b49172` | Preloaded excerpts, structured replacements | 16,145 | 6.23 | 81.86 | Full regression accepted |

All three experimental workers together consumed **105,061 tokens**, which is **23,109 more** than the historical single run. The 80.3% reduction describes the final successful cycle only; the optimization session as a whole did not save tokens against that single-run baseline. Coordinator usage was not measured. An initial launch stopped at the existing writer lease before invoking a worker and consumed zero worker tokens.

The checkout's interactive writer lease remained held, so demonstrations ran from separate reviewed source snapshots under `/tmp`, each with its own lease, Git checkpoint/worktree and linked existing dependencies. No lease was bypassed and no unrelated writer was interrupted. Local evidence was copied back to the Git-ignored private report directory:

- [Measurement summary](../qa-reports/focused-repair-measurements.json)
- [Accepted report](../qa-reports/20261109T124700Z-b49172/report.json), [worker usage metadata](../qa-reports/20261109T124700Z-b49172/worker-1.json), [exact repair patch](../qa-reports/20261109T124700Z-b49172/worker-1.patch)
- Rejected measurements: [52,550 tokens](../qa-reports/20261109T122400Z-3c6a3f/report.json), [36,366 tokens](../qa-reports/20261109T124416Z-e51753/report.json)
- [Shutdown verification](../qa-reports/focused-repair-shutdown-validation.json): Ctrl+C and `--stop` each passed, retained reports/browser journals, exited 130 and closed the owned port. This ran against the same isolated source snapshot after the accepted demo.
- [Final report-only validation](../qa-reports/20261109T125216Z-7db1fa/report.json): after adding the focused-browser rebuild prerequisite checks, all **150 Python tests (including 59 QA tests), 12 browser tests, typecheck and build** passed with zero failures/skips. This final run invoked no worker and used zero worker tokens.

The shutdown verifier requires free port 8876. It interrupts the real browser fixture with Ctrl+C and `--stop`, checks exit 130, retained reports and closure of the owned fixture port. These intentional interruptions must not be interpreted as failing application tests.

Physical Wayland clipboard, external labs, prolonged GPU/VRAM soak and minimum-version Python remain outside this workflow. Missing optional host binaries are explicit skips, never counted as passes.
