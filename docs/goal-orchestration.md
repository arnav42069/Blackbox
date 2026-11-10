# Goal-based orchestration repair

The follow-up [autonomous room workflow](autonomous-room-workflow.md) documents
target invalidation, explicit objective criteria, adaptive port batches, VPN
preflight, linked conversation evidence, and current acceptance-test results.

The original failure had two causes: the system prompt preferred SSH before
service investigation, and the general tool loop treated every “Needs access”
result as a terminal state. A room/flag goal could therefore stop after its first
SSH proposal, even though approved network and HTTP tools remained available.

`core/goals.py` adds a typed, persistent goal controller over the existing planner,
scan repository and registry. Room-solving and flag-retrieval requests decompose
into evidence retrieval, service prerequisites, public HTTP inspection/content
discovery, optional existing SFTP access, and file verification. Explicit requests
to use an SSH profile retain the existing tool workflow.

The controller:

- Reuses current scoped HTTP/service observations, including nonstandard ports,
  before requesting Nmap. It scans only missing authorized ports, up to the existing
  1000-port cap, with service detection and server-generated arguments.
- Inspects supplied/known HTTP endpoints, tests the requested public file candidate,
  uses bounded content discovery when permitted, and follows same-origin links or
  discovered directories to test candidate paths. Multiple known HTTP services can
  be investigated within the same run budget.
- Records actual response evidence and hypotheses after each action. A plain HTTP
  200/soft-404 is not a flag. HTTP flag candidates require recognizable flag syntax
  or a single 32-character hex value at the requested file path. SFTP reads report
  actual located file contents. Neither route proves challenge acceptance.
- Uses only configured read-only SFTP after public discovery. Missing credentials,
  host keys or access are tool-local limitations; other permitted actions remain
  eligible. No credential collection, authentication bypass, exploitation, shell,
  remote writes or privilege escalation was added.
- Uses `Registry.execute` for every action, preserving scope attestation, DNS/port
  validation, manual approvals, rate limits, request/tool budgets, fixed subprocess
  arguments, cancellation and the evidence journal.

## Persistence and continuation

SQLite `task_goals` stores the immutable user instruction, resolved target,
subgoals, attempted action keys, hypotheses, limitations, candidate paths, pending
action, verified result provenance and checkpoint counters. It is session-owned
and deleted with the session. `/api/sessions/{sid}` exposes `task_goal`; Activity
shows goal actions, observations and inspectable checkpoints.

On tool-call, HTTP-request, planner-step or wall-time exhaustion, BlackBox saves
progress, reports what remains, and offers **“Continue task”** (also **“Resume goal”**).
This explicit user message starts exactly one new bounded run. Nothing continues
in the background; completed actions are not automatically repeated. All current
scope and policy checks run again. Authorization changes invalidate the prior
execution plan. Cancellation saves a cancelled checkpoint. Restart marks active
goals interrupted and revokes scope; continuation requires renewed authorization.
A completed goal retains its evidence timestamps rather than pretending a new read
occurred. Exhaustion of the current permitted discovery plan remains an unfinished
goal with stated limitations, not fabricated room completion.

## Regression coverage

`tests/integration/test_goals.py` uses mocked Nmap/SSH outputs and isolated real
loopback HTTP fixtures. The seeded premature SSH proposal is never needed in the
new room-goal path. Tests cover:

- Discovery → HTTP → verified candidate, with no SSH credentials/model tool calls.
- Existing evidence reuse and supplied-URL inspection without an unnecessary scan.
- A mocked SSH access failure followed by real loopback HTTP investigation.
- Content discovery after the direct flag path returns 404.
- Multiple known HTTP services and soft-404 rejection.
- Tool, request, planner-step and wall-time checkpoints.
- Explicit continuation without repeating completed scans/reads.
- Out-of-scope rejection and scope revocation before continuation.
- Scanner failure without fabricated access/flags.
- Cancellation, durable reload, restart interruption and no automatic resumption.

The behavior remains limited to the application's existing permitted tools. Rooms
requiring exploitation, unsupported protocols or unavailable authorized access may
remain unsolved; the agent reports evidence and a checkpoint instead of promising
or inventing a solution. QA orchestrator and token-budget optimization code is
unchanged. Deterministic goal planning adds no planning/answer LLM calls.

## Verified results

- `.venv/bin/python -m pytest -q`: **219 passed, 0 failed, 0 skipped** (22.16 s),
  including 18 new goal-controller/routing cases and the existing QA tests.
- `npm --prefix frontend run typecheck`: passed.
- `npm --prefix frontend run build`: passed.
- `npm --prefix frontend run test:e2e`: **13 passed, 0 failed** (9.7 s).
- `git diff --check`: passed.

Files added: `backend/blackbox/core/goals.py`, `tests/integration/test_goals.py`,
`docs/goal-orchestration.md`.

Files extended: `backend/blackbox/core/agent.py`, `backend/blackbox/core/planner.py`,
`backend/blackbox/storage/db.py`, `backend/blackbox/app.py`,
`frontend/src/components/Activity.tsx`, `prompts/agent_system.txt`, and `README.md`.
Existing uncommitted work was preserved; QA scripts/token optimizations were not edited.
