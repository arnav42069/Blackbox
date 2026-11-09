# BlackBox implementation report — 2026-11-09

The complete `BlackBox_PRD_v1.1_BlackTurq.md` was read as the implementation source.
This directory has no Git repository, so no commit/diff history was available.
Existing files were continued rather than replaced with a fresh project.

## Starting state and completed work

The prior session provided the Python package, basic FastAPI routes, pairing/auth,
SQLite schema, direct Ollama client, CLI doctor and BlackTurq CSS. It did not have
the imported frontend App component, functional image uploads, executable tools,
scope enforcement, tool iteration, install scripts or tests. The inherited message
insert also used eight placeholders for a seven-column table and could not save chat.

The implementation now includes:

- Functional Preact chat, model selection/capability warnings, persistent sessions,
  sanitized Markdown, keyboard controls, responsive session/activity drawers,
  accessible scope/settings dialogs, image paste/drop/upload previews, Retry and Stop.
- Pillow-sniffed image normalization, metadata removal and multipart validation,
  transactional attachment/message/run creation, session-specific image serving and
  actual Ollama image-base64 mapping. Failed sends preserve drafts; Retry reuses images.
- A registered typed tool loop with sequential execution, single repair attempt,
  call/turn/time/context/output/request budgets, evidence feedback and cancellation.
  Tool history is repaired after partial multi-call cancellation; older context is
  compacted without orphaning call/result pairs or disclosing private reasoning.
- Real host binary/version detection, bounded workspace list/read/search, ping,
  TCP Nmap scanning, pinned HTTP inspection, curated ffuf path discovery, authorized
  read-only SSH/SFTP probe/list/find/read, and new-file session artifacts.
- Direct user scope attestation, TTLs, strict target/port parsing, DNS address checks
  and pinning, redirect checks, no proxy/config bypass, private/secret path exclusions,
  known-host verification and existing public-key access only. Target tools serialize
  across sessions; scope TTL also bounds long subprocesses.
- Actual argv events, timestamps, stdout/stderr, exit/duration/status, tool IDs,
  redaction, explicit denials, and verified SSH read citations added to final answers.
- Authenticated resumable SSE, atomic private storage, stale-run/tool interruption,
  revoked scopes on restart, session deletion/export, event/tool-log retention caps,
  age-based session retention and failed-upload orphan cleanup. Old screenshots
  are not loaded/base64-encoded for unrelated follow-up requests.
- User-local installer/launcher, dependency locks, working CLI doctor/probes, dev
  build/reload script, smoke commands, security/tools/design/architecture documentation
  and browser-generated screenshots at all three required viewport sizes.

The existing architecture and source palette were preserved. Browser GET auth was
fixed to use a CORS-protected custom origin header when browsers omit the forbidden
Origin header. Foreign Origin values always fail. Diagnostic DB connections no
longer interrupt an active server's jobs. Normal CLI launch holds a data-directory
lock and checks the port before startup recovery.

## Verification actually executed

Environment: Omarchy/Arch Linux, Python 3.14.8 (project supports 3.12+), installed
system Chromium, local Ollama. No external target was contacted by tests.

| Command/check | Result |
|---|---|
| `uv pip install --python .venv/bin/python -e '.[test]'` | Installed Python dependencies into the existing venv |
| `./scripts/install_omarchy.sh` | Passed; pinned deps, static build, `~/.local/bin/blackbox` wrapper installed |
| `.venv/bin/python -m pytest -q` | **71 passed, 0 failed, 0 skipped**; full suite ~4.5 s |
| `npm --prefix frontend run typecheck` | Passed |
| `npm --prefix frontend run build` | Passed; final JS ~106.6 kB / 37.5 kB gzip |
| `(cd frontend && npm run test:e2e)` | **12 passed, 0 failed, 0 skipped**; latest run 9.1 s |
| `npm --prefix frontend audit --audit-level=moderate` | 0 vulnerabilities after updating Preact, DOMPurify and Vite |
| `uv pip check --python .venv/bin/python` | All installed packages compatible |
| Dev script smoke (intentional stop after 5 seconds) | Vite watch and backend ready; no traceback or surviving watcher/server |
| `bash -n scripts/*.sh` and Python compile/import checks | Passed |
| `.venv/bin/python scripts/cli_smoke.py` | Installed launcher served UI over real HTTP; pairing/auth, health, real model discovery and session creation passed |
| `.venv/bin/python scripts/real_model_smoke.py` | Actual installed 4B model passed vision + tool probes, identified uploaded red image, called real host.preflight and consumed result; latest measured smoke 6.0 s |
| `blackbox doctor --model huihui_ai/qwen3.5-abliterated:4b --probe --json` | Actual tools=true and vision=true, cached by model digest in normal app DB |

Backend tests include out-of-scope targets/ports, DNS rebinding and expiry during
DNS resolution, active-scan policy, injection/unknown flags/tools, workspace
traversal/symlinks, image size/type/dimensions/metadata, malformed call repair,
multiple calls/budgets, concurrent sends, unavailable Ollama, auth/Origin/Host,
manual approvals, cancellation of descendants, retry/image reuse, private storage,
restart interruption, diagnostic connections and retention.

HTTP, ping, Nmap and ffuf integration tests execute against explicitly authorized
localhost fixtures using actual installed binaries. Public-key SSH/SFTP tests
create an ephemeral trusted local server with `/home/lab/user.txt`. The agent's
probe → find → read chain retrieves the actual planted flag and forwards it to
the model observation; secret symlinks and changed host keys are rejected. Missing
profiles stop with `needs_access`, without invented flags or fallback exploitation.
The fixture disables GSS hostname lookup to avoid this machine's slow local FQDN
resolution; host-key checks remain enabled.

Browser tests cover synthetic PNG clipboard delivery through a real backend tool
loop, text paste preservation, saved history, scope denial, Stop, Retry, unavailable
Ollama, SSE reconnect without rerunning tools, keyboard dialogs, exact BlackTurq
colors, AA contrast of text/status/action pairs, reduced motion, focus and no page
overflow at 1440×900, 1024×768 and 390×844. Screenshots are in `docs/screenshots` and
use a clearly identified local protocol fixture. System Chromium was used; no
Playwright browser download is required. Node's Playwright loader currently emits
a nonfatal deprecation warning on the installed Node version.

Measured installed backend RSS after real model discovery: **82.8–82.9 MiB**, excluding
Ollama/browser, below the PRD's best-effort 250 MiB target. This is a single idle
sample, not a long-term memory benchmark.

## Explicit limits and outstanding verification

- Physical Wayland Ctrl+V remains a manual check; synthetic browser PNG paste and
  actual byte delivery were verified automatically.
- External SSH/VPN routing and the user's real lab credentials were not tested.
  The SSH fixture uses actual SSH/SFTP protocol and key verification on localhost.
- Fifty-turn GPU/RAM soak, 8 GB VRAM profiling and prolonged operation were not
  measured. Supported minimum Python 3.12 was not separately tested on this host.
- Final answers arrive in complete blocks; activity/status/output uses live SSE.
  Final token streaming, optional grim capture, warm-up and reusable scope profiles
  remain future work. No automatic interrupted-command resume is implemented.
- HTTPS public path discovery intentionally uses the pinned, certificate-validating
  HTTP adapter instead of ffuf's permissive TLS transport. HTTP discovery uses real
  ffuf with a curated 12-entry list, fixed budgets and no redirects/recursion.
- `whatweb` is absent here and only diagnosed; HTTP inspection is implemented via
  httpx. The PRD default `qwen3.5:4b` is not installed; no model was downloaded.
  The already-installed `huihui_ai/qwen3.5-abliterated:4b` passed real probes.
- Secret redaction is heuristic; the local OS user and remote SSH server remain
  trust boundaries. Model prose is not proof of execution; use the evidence cards.

## Run now

Installation is already complete. From a terminal:

```bash
cd /home/arnav/Projects/blackbox
~/.local/bin/blackbox doctor
~/.local/bin/blackbox --model huihui_ai/qwen3.5-abliterated:4b
```

Use the printed one-use pairing link if the browser does not open. The app is served
at `http://127.0.0.1:8765`. The real 4B capability probes are cached in the normal
application DB, so this model can use vision/tools immediately. Scopes must still
be explicitly authorized in the UI; probes grant no network access.

For backend-only launch use `--no-browser`; for a different port use `--port 8766`.
For installation on another checkout, run `./scripts/install_omarchy.sh` first.
For development use `./scripts/dev.sh`; for the deterministic local SSH demo:

```bash
.venv/bin/python -m pytest -v tests/integration/test_ssh_fixture.py::test_agent_retrieves_real_sftp_flag
```

The demo creates and removes its own lab, trusted host key, temporary client key,
scoped target and planted flag. It does not read the user's real SSH credentials.
