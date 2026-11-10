# Prompt 1 — Fix tool arguments and error recovery
Priority: Critical · Fix the immediate crash

Fix BlackBox's tool-call argument handling and recovery mechanisms.

Observed failure from the real UI:
- workspace.read was invoked with an invalid path.
- The tool reported: "Workspace paths must be relative, without traversal."
- BlackBox repeated the failed operation and stopped with "workspace.read failed twice without new evidence."
- An earlier run similarly stopped after repeated web.inspect failures.
Implement the following changes in the existing codebase:
1. Trace the complete Ollama tool-call lifecycle from model generation through argument parsing, validation, execution, error handling, and continuation.
2. Introduce typed, validated schemas for every available tool.
3. Ensure the model receives accurate tool schemas, valid argument examples, and permitted argument constraints.
4. Where an argument can be repaired unambiguously and safely, normalize it before execution. Otherwise return a structured, actionable validation error to the planner.
5. For workspace.read, permit only paths relative to configured workspace roots. Reject traversal, symlink escapes, and unauthorized absolute paths.
6. Classify errors into validation errors, policy denials, transient execution failures, permanent failures, and unavailable tools.
7. Never blindly retry an identical failed call. After a repeated deterministic error, select a genuinely different valid action.
8. Preserve the objective and evidence when changing strategies.
9. Ensure every terminal failure generates a clear assistant message explaining the blocker and possible next action.
Add deterministic regression tests covering malformed arguments, repeated failures, safe normalization, and recovery.

Do not weaken authorization, workspace isolation, or policy enforcement.

Implement and run the tests. Report modified files, test results, and remaining failures.
Prompt 2 — Implement an autonomous planner-executor-observer loop
Priority: Critical · Fix the agent stopping prematurely

Upgrade BlackBox's autonomous task orchestration so that it can complete multi-step objectives instead of stopping after initial enumeration.

The current behavior is inadequate: BlackBox scans a host, records evidence, encounters a tool failure, and terminates without reaching the user's original objective.

Implement a robust, bounded planner-executor-observer architecture.

Requirements:
1. Maintain an explicit objective throughout the entire session.
2. Track completed subtasks, pending subtasks, hypotheses, observations, blockers, and evidence references.
3. After every tool result, evaluate what was learned and select the next useful action.
4. Allow the planner to dynamically revise its approach based on new evidence.
5. Detect repeated or equivalent actions and prevent unproductive loops.
6. Distinguish task completion from successful tool execution.
7. Require evidence-backed completion criteria.
8. Persist checkpoints so interrupted tasks can be continued.
9. Expose a configurable step budget, tool budget, and wall-clock budget.
10. On budget exhaustion, save progress and explicitly offer continuation.
11. Stop only on verified completion, cancellation, policy denial, exhausted budget, or a genuine blocker.
12. When an action fails, inspect the failure and attempt an appropriate alternative within the authorized scope.
13. Use previously collected evidence to avoid redundant scans, but refresh observations when required.
14. Keep the user informed with concise progress messages.
Critical test scenario:

An authorized lab target exposes HTTP on port 80 while ports 443, 8080, and 8443 are closed.

The agent must:
- Recognize port 80 as an active HTTP service.
- Investigate that service using authorized tools.
- Use returned evidence to select further relevant actions.
- Avoid retrying inaccessible ports without justification.
- Continue until completion, budget exhaustion, or an actual blocker.
Use mocked local services for deterministic testing.

Preserve strict scope enforcement and all existing safety controls.

Implement the changes, run the tests, and report the results.
Prompt 3 — Make HTTP investigation adaptive
Priority: High · Fix the TryHackMe investigation workflow

Improve BlackBox's authorized web-service investigation workflow.

Observed real lab results:
- Target: 10.49.170.55 (authorized TryHackMe machine).
- 80/tcp: open, Apache HTTP server.
- 443/tcp: closed.
- 8080/tcp: closed.
- 8443/tcp: closed.
- GET http://10.49.170.55:80/ returned HTTP 200.
- BlackBox subsequently stalled and failed to complete the investigation.
Implement an evidence-driven HTTP reconnaissance workflow.

Requirements:
1. Parse successful Nmap output into structured service records.
2. Automatically make discovered in-scope HTTP services available to the planner as investigation candidates.
3. Inspect HTTP headers, titles, redirects, response content, and relevant service metadata.
4. Support bounded, read-only discovery of common public paths and application resources.
5. Extract and normalize in-scope links from retrieved HTML.
6. Investigate relevant client-side JavaScript resources when permitted.
7. Detect useful application characteristics, frameworks, exposed metadata, and unusual endpoint behavior.
8. Rank investigation steps by information gain rather than blindly running every tool.
9. Feed tool observations back into the planner as structured evidence.
10. Avoid repeated requests to unchanged resources without justification.
11. Correctly handle nonstandard HTTP ports, redirects, timeouts, and HTTP errors.
12. Never automatically expand to out-of-scope hosts or ports.
13. Enforce existing rate limits, request budgets, and authorization rules.
Add a local mock HTTP lab containing multiple pages, redirects, linked resources, and a discoverable public information artifact.

Create an end-to-end test demonstrating that BlackBox progresses from an initial port scan through multi-step HTTP investigation and correctly reports the artifact with supporting evidence.

Implement, test, and document the workflow.
Prompt 4 — Fix Ollama reasoning, tool-calling and missing responses
Priority: High · Make the local model behave reliably

Audit and repair BlackBox's Ollama integration, specifically for Qwen 3.5 models and models that return separate thinking, content, and tool_calls fields.

Current selected model:
huihui_ai/qwen3.5-abliterated:4b

Observed problem:
BlackBox executes tools and displays Live Activity, but sometimes produces no meaningful assistant response and transitions to FAILED or IDLE.

Requirements:
1. Inspect the Ollama request/response integration end-to-end.
2. Correctly handle thinking, content, tool_calls, streaming chunks, finish reasons, and errors.
3. Never discard valid assistant content because thinking content is also present.
4. Preserve tool-call context across successive model requests.
5. Verify that tool responses are appended with the correct roles and message structure.
6. Distinguish an empty intermediate assistant message from a completed response.
7. Implement bounded recovery for empty or malformed model outputs.
8. Provide a reliable fallback response if the model cannot generate a final answer.
9. Add structured debug logging for model generation, tool calls, token usage, and orchestration state.
10. Introduce a model capability probe that tests structured tool calling, multi-turn tool use, and final answer generation.
11. Display a compatibility warning when a model cannot reliably execute the required tool workflow.
12. Avoid assuming all Ollama models provide identical reasoning or tool-calling behavior.
Build mocked Ollama response tests covering:
- Thinking-only chunks followed by content.
- Tool calls followed by tool results.
- Multiple consecutive tool calls.
- Empty responses.
- Malformed tool arguments.
- Streaming interruptions.
- Successful final response generation.
Implement the fixes and report test results.

Do not suppress model errors or fabricate responses to tool executions.
Prompt 5 — Build persistent progress, checkpoints and live diagnostics
Priority: Medium · Make autonomous execution observable

Upgrade BlackBox's frontend and backend to provide transparent, persistent autonomous-agent progress tracking.

Currently the UI shows tool executions, but it does not adequately explain why the agent selected an action, what objective remains outstanding, or why execution stopped.

Implement:
1. A live objective tracker displaying the user's current goal.
2. A task list showing pending, running, completed, blocked, and failed actions.
3. A live activity feed with tool name, sanitized arguments, execution status, duration, and evidence references.
4. A concise explanation of why each next action was selected, without exposing hidden model reasoning.
5. A visible indication of the current planner state.
6. Explicit terminal states: COMPLETED, BLOCKED, FAILED, CANCELLED, and BUDGET_EXHAUSTED.
7. A Continue button that resumes execution from a persisted checkpoint.
8. Pause, Resume, and Cancel controls with correct backend cancellation semantics.
9. Configurable tool-call, execution-step, and time budgets.
10. A session timeline showing discoveries and completed objectives.
11. Reliable streaming of progress updates to the frontend.
12. Backend persistence so progress survives frontend refreshes.
13. Clear warnings when a session's authorization scope has expired or was revoked.
14. A final evidence-based report explaining the actual outcome.
Ensure that refreshing the browser never silently creates a duplicate execution.

Add integration and frontend tests.

Preserve the existing terminal aesthetic and safety architecture.

Implement and test all changes.
Prompt 6 — Build a complete automated TryHackMe-style QA benchmark
Priority: High · Verify that the fixes work together

Create a comprehensive, repeatable end-to-end QA benchmark for BlackBox's autonomous authorized lab investigation capabilities.

The objective is to verify that BlackBox can solve a realistic multi-step lab task rather than merely execute individual tools.

Build a self-contained local mock lab that requires the agent to:
1. Receive an authorized target and a natural-language objective.
2. Discover a reachable service.
3. Identify its protocol and application.
4. Inspect relevant content.
5. Discover and follow evidence-based investigation paths.
6. Recover safely from at least one tool argument error.
7. Recover from a transient tool execution failure.
8. Avoid repeated actions that produce no useful information.
9. Locate a deliberately planted, publicly accessible test flag.
10. Return the exact flag with verifiable evidence.
11. Mark the objective completed only after verifying the result.
Also test:
- Authorization denied.
- Out-of-scope target.
- Expired authorization.
- Tool-budget exhaustion.
- Repeated deterministic tool failures.
- Model returning no final content.
- Backend interruption and checkpoint recovery.
- Frontend refresh during execution.
- Model lacking tool-calling capability.
Use a disposable local lab with a known synthetic flag. Do not test against uncontrolled external targets.

Run focused unit tests, integration tests, frontend tests, and the end-to-end benchmark.

Use the repository's existing QA framework where possible.

Report:
- Test results.
- Objective completion rate.
- Tool-call counts.
- Redundant action counts.
- Recovery behavior.
- Remaining limitations.
- Files changed and commits.
Fix any regressions introduced by this work.

Do not claim the benchmark passes unless the complete agent actually reaches the planted flag and returns it successfully.


# Prompt 2 — Fix BlackBox's broken process lifecycle commands.

CURRENT BUG:
Running 'blackbox kill' returns:
"an owner not visible through /proc holds backend-instance lock .../server.lock. No locked file was removed."
It refuses to stop BlackBox.

REQUIREMENTS:
1. Investigate the actual CLI entrypoint, backend startup, process ownership, and file-lock implementation.
2. Implement reliable 'blackbox kill', 'blackbox restart', and 'blackbox status' commands.
3. 'blackbox kill' must identify the actual BlackBox backend process, request graceful shutdown with SIGTERM, wait for a bounded period, and escalate to SIGKILL only when ownership has been verified.
4. Do not refuse shutdown solely because a lock is held.
5. Never kill unrelated uvicorn, Python, or web server processes.
6. Track process identity using PID, process start time, executable/command identity, and an instance identifier where appropriate.
7. Properly handle orphaned children, detached processes, stale PID files, inaccessible /proc entries, stale locks, and permission errors.
8. Use a single-instance ownership mechanism that prevents duplicate backends and handles races safely.
9. Do not delete an actively held lock file to bypass ownership checks.
10. 'blackbox restart' must stop the existing verified instance, verify shutdown, then launch a fresh instance and confirm health.
11. 'blackbox status' must report actual process, lock ownership, port state, and health-check results.
12. Support instances started from the CLI, systemd user services, and development launchers where applicable.
13. Add deterministic tests for active locks, stale locks, inaccessible owners, graceful termination, forced termination, and restart.
14. Preserve existing user data, sessions, and agent execution checkpoints.
15. Run the tests and report actual results.

IMPORTANT: Do not implement a blanket pkill -f 'blackbox|uvicorn'. Avoid terminating unrelated processes.

Fix the root cause, not just the error message.