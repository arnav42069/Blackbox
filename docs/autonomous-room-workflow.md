# Autonomous authorized-room workflow

BlackBox now treats artifact retrieval as an objective with explicit completion
criteria, rather than treating reconnaissance success as task completion. It
extends the existing goal controller, planner, scan store, policy engine, and SSE
conversation protocol. It does not add a second agent framework or a shell tool.

## Using the workflow

1. Attest authorization in Scope. Select one exact target IP and approved TCP
   ports. Enable active scanning only when authorized. A scope CIDR does not cause
   automatic host expansion.
2. Ask, for example: `Solve the authorized TryHackMe lab at 10.49.170.55 and
   retrieve flag.txt`. For a location-specific objective, state `Retrieve flag.txt
   from the authorized machine's user home directory` and provide the exact IP.
3. BlackBox retrieves same-target, same-authorization evidence and checks missing
   prerequisites. Before new target operations it checks routing and bounded ICMP
   reachability inside the tool execution timeout. Cached evidence is not a new
   network operation. A silent ICMP probe is inconclusive, so bounded authorized
   TCP/HTTP discovery may proceed. Missing routing blocks enumeration.
4. The controller persists PLAN → EXECUTE → OBSERVE → EVALUATE → REPLAN transitions,
   the objective, exact target, hypotheses, observations, pending action, evidence
   references, remaining call/request/time budgets, and completion criteria.
5. Common approved web ports are prioritized. When they yield no useful service,
   remaining **approved** ports are scanned in disjoint batches of at most 1,000.
   Compact arguments retain existing schema limits. An unproductive known web
   service also does not prevent later approved port batches being investigated.
6. Observed HTTP services, including nonstandard ports, are inspected. The loop
   follows bounded same-origin links, reads candidate artifact paths, and uses
   ffuf/native bounded content discovery when active scanning is allowed. Available
   WhatWeb and OpenSSL adapters can add fingerprint/handshake evidence after direct
   artifact investigation. Technology names are not vulnerability confirmation.
7. On a limit, BlackBox saves the pending action and explains the incomplete
   objective. Click **Continue goal**, or send `Continue task` / `Resume goal`,
   for one new bounded run. Fresh completed work is reused; stale work requires
   revalidation. Failed actions can be retried on a later explicit continuation.
   Stop cancels execution; no continuation happens in the background.

The objective panel shows the current phase and budget. Human-readable milestones
and final answers retain target labels; collapsible tool summaries beneath them
link to evidence IDs, outcomes and timestamps. Activity retains raw evidence and
route/state events. Retry and persistent session reload remain supported.

## Target changes and evidence

Changing authorization invalidates pending goal assumptions, candidates, verified
findings, and any completion result. Prior raw evidence and conversation remain
historical; the UI displays a target/authorization-change notice. A new target
instruction creates a new goal identity. Continuing an old goal against a scope
which excludes its target is rejected, rather than silently selecting another host.

Policy validation restricts every goal tool to its run's exact IP, even when the
session scope allows several hosts. HTTP redirects are subject to the same rule.
Evidence carries its authorized target, original run and observation time. Cached
evidence keeps original provenance and identifies the run which reused it.
The goal controller does not use another target's scan results as current findings.

## Completion and limitations

- Exit code zero with no open services is an execution outcome, not investigation
  progress. HTTP 404 is retained as an observation without satisfying the objective.
- A generic artifact goal completes only after an actual bounded file-candidate
  read returns supported flag content. A soft-404 or discovered filename is not
  sufficient. The response quotes actual contents and evidence provenance.
- A **home-directory** objective requires an actual matching file read under
  `/home/` through configured authorized read-only SFTP. A public HTTP flag candidate
  cannot prove that location. Missing SSH access limits SFTP; permitted public
  discovery is still considered independently.
- Artifact retrieval does **not** verify TryHackMe submission acceptance or every
  room objective. BlackBox explicitly states that distinction.
- TryHackMe route validation recognizes loopback fixtures and tunnel interface
  families `tun`, `tap`, `wg`, `tailscale`, and `ppp`. Other VPN/AttackBox topologies
  cannot currently be verified automatically and produce a routing blocker. If
  `iproute2` is unavailable, local route selection can be checked without sending
  a UDP packet, but a non-loopback VPN interface cannot be verified that way.
- Supported investigation remains bounded scans, HTTP inspection/discovery,
  fingerprinting, TLS checks and configured read-only SFTP. Rooms needing exploits,
  credential attacks, arbitrary shell access, privilege escalation, unsupported
  protocols or challenge submission remain blocked with evidence and next steps.
  No such capabilities or success claims were added.

## Verification

Deterministic room acceptance tests never contact the example private addresses:
their discovery, VPN and HTTP adapters are mocked. A separate existing integration
test uses real Nmap, ffuf and an ephemeral loopback HTTP fixture.

```bash
.venv/bin/python -m pytest -q tests/integration/test_room_objectives.py
.venv/bin/python -m pytest -q tests/integration/test_room_objectives.py \
  tests/integration/test_goals.py tests/integration/test_planning.py \
  tests/unit/test_intent_parsers.py --junitxml=qa-reports/room-focused.xml
.venv/bin/python -m pytest -q tests/integration/test_room_objectives.py \
  tests/integration/test_goals.py tests/integration/test_goal_live.py \
  --junitxml=qa-reports/room-goals.xml
.venv/bin/python -m pytest -q --junitxml=qa-reports/room-full.xml
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run test:e2e
git diff --check
```

Verified focused results: **9/9** new room tests; **74 passed** in the focused
planner/parser suite; **30 passed** including the real local Nmap/ffuf workflow.
Cases cover the exact target switch `10.49.150.109` → `10.49.170.55`, nonstandard
HTTP, linked multi-step flag retrieval, budget interruption/resume without rescan,
home-directory provenance, exact-target rejection, VPN/ICMP behavior, and scanning
beyond the initial 1,000-port batch. Frontend unit tests: **14 passed**; production
build and TypeScript checks passed. Full backend/browser reruns are recorded below.

Final reruns: **327 backend tests passed, 2 skipped** in 88.32 seconds;
**19 browser tests passed** in 17.8 seconds. The skipped tests are the separately
opted-in actual-Ollama conversation tests; they were not needed or rerun for this
deterministic room-controller change. The real local Nmap/ffuf integration did run
and pass. `git diff --check` passed. No final-run failures remain.

Testing caught two regressions introduced during implementation: the objective UI
initially dereferenced a null session (all 19 browser tests failed at startup), and
expanded normalized port lists exceeded the live argument parser's character
bound (one new large-range test failed). Both were fixed and affected suites rerun.
Normalized evidence lists are decoded only as evidence; executor arguments remain
compact and schema-validated.

## Changed files and compatibility

Backend: `core/goals.py`, `core/planner.py`, `core/scope.py`, `core/policy.py`,
`core/scans.py`, `core/agent.py`, `app.py`, `tools/network.py`, `tools/web.py`,
`tools/registry.py`, `tools/runner.py`, and `processes.py` (recognize the new fixed
`ip` subprocess during backend-owned child cleanup).

Frontend: `src/app.tsx`, `components/Messages.tsx`, `components/Activity.tsx`.
Tests: new `tests/integration/test_room_objectives.py`, extended
`tests/e2e/server.py` and `tests/e2e/chat-tool-loop.spec.ts`. Documentation:
`README.md`, `docs/goal-orchestration.md`, and this workflow.

New goal fields have defaults for existing saved sessions; no database schema
replacement is required. Historical evidence is retained. Existing scope, rate,
request/call/time limits, tool validation, cancellation and audit logging remain
authoritative. Deterministic goal planning works without native model tool calls
and does not spend extra LLM tokens. The existing QA orchestrator was not modified.
The frontend is rebuilt. Restart/reload to apply changes to a running installation;
restart still revokes scopes and pairing credentials.
