# BlackBox

A local Ollama chat workbench for explicitly authorized labs, built with FastAPI,
SQLite, Preact and the Omarchy BlackTurq palette. Tools execute real bounded
operations on your workstation. Authorization is enforced by Python, independently
of the model. There is no arbitrary shell tool.

## Install and run

From this directory, with Python 3.12+, Node and npm installed:

```bash
./scripts/install_omarchy.sh
~/.local/bin/blackbox doctor
~/.local/bin/blackbox
```

The installer builds the UI, installs pinned Python/frontend dependencies, and
creates a user-local launcher. It preserves existing data and preferences. It does
not change shell rc files, SSH configuration, desktop settings or system services.
If needed, add the launcher to this terminal's PATH:

```bash
export PATH="$HOME/.local/bin:$PATH"
blackbox --no-browser
```

Open the one-use pairing link printed in the terminal (expires after 120 seconds).
Only `http://127.0.0.1:8765` is accepted. A bare browser tab displays pairing
instructions. Pairing tokens live in URL fragments, are exchanged once, and are
removed from browser history; the API uses a bearer token in sessionStorage.
Backend restart requires a new pairing link. Do not share that link.

Ollama must already be installed and running. No model is downloaded automatically:

```bash
ollama list
ollama serve                           # only if not already running
ollama pull qwen3.5:4b                  # optional explicit multi-GB download
blackbox --model qwen3.5:4b
blackbox --port 8766 --no-browser
```

You already have `huihui_ai/qwen3.5-abliterated:4b` installed on this workstation.
It passed real vision and tool smoke tests during implementation. To use it:

```bash
blackbox --model huihui_ai/qwen3.5-abliterated:4b
```

Select it in the model menu, or Settings on a phone, and use **Settings → Probe
vision + tools** once. Modified models stay unverified until their own probes pass.
CLI alternative: `blackbox doctor --model huihui_ai/qwen3.5-abliterated:4b --probe`.
Probe results are cached by digest in the normal application database. The
implementation smoke uses temporary data and does not preauthorize your sessions.
A model without tools still supports ordinary text chat; a model without verified
vision cannot receive images. Changing the current model preserves the transcript.

On Arch/Omarchy, these packages provide the basic host tools:

```bash
sudo pacman -S --needed python python-pip nodejs npm curl openssh nmap iputils
# Optional, if available from your configured repositories:
# sudo pacman -S --needed ffuf whatweb grim wl-clipboard chromium
```

Missing tools are reported by `doctor` and omitted from model tool schemas. `whatweb`
is detected for diagnostics but has no separate executor; HTTP inspection uses
httpx. Clipboard images use browser APIs; neither grim nor wl-clipboard is required.

## Chat, screenshots and evidence

Type a message and press Enter; Shift+Enter inserts a newline. Paste a screenshot
with Ctrl+V, drop it on the composer, or choose **+ Image**. Up to four PNG/JPEG/WebP
images, each at most 8 MiB and 4096 px/side, are validated and normalized privately.
Previews can be removed before sending. Failed sends preserve your draft/images.
The image bytes go to Ollama's `images` field, including mixed image/tool turns.

**RUN** starts a bounded model/tool loop. **STOP** cancels model requests and kills
subprocess groups. **Retry last message** starts a fresh bounded run with the previous prompt and
its original attachments, using the current model and scope. The live activity pane shows real tool arguments,
output, executed argv, exit status, duration, denial reasons and tool IDs.
Successful SSH reads add verified target/path/tool/timestamp/snippet evidence to
the final answer. Model commentary is
labeled separately. On smaller screens use Activity/Sessions drawers.

Sessions persist across restarts. Stale runs/tools become `interrupted`; no command
resumes automatically. Export saves sanitized transcript JSON; Delete session
removes its messages, images, journal and artifacts. Retention (default 30 days)
is applied on startup and when settings change. Workspace notes are independent
of session deletion.

## Authorize a lab

Before using a target tool, click **Scope** and enter exact IPs/CIDRs or hostnames,
allowed TCP ports, approved read roots, expiry, request budget and whether bounded
active scans are allowed. Check the explicit authorization attestation. Private IPs
and prompts mentioning TryHackMe never grant authorization. Scopes are revoked on
backend restart or session deletion; select a short TTL for unattended sessions.

Auto safe mode runs preauthorized read-only tools directly. Port scans and web path
discovery additionally require the active-scan checkbox. Manual mode asks once per
tool in the activity pane. Neither mode permits remote writes, auth attacks, scope
changes by a model, arbitrary shell commands or host privilege escalation.

Examples after appropriate authorization:

- “Use host.preflight and summarize installed tool availability.” (No target scope.)
- “Inspect HTTP on the approved lab target and cite the response status.”
- “Check ports 22,80 on my approved target.” (Active scanning enabled.)
- “Using my lab SSH profile, find user.txt under /home and read the candidate.”

HTTP destinations are resolved, checked and pinned; redirects are rechecked before
following. Nmap runs a bounded TCP connect scan with server-generated flags. Public
path discovery uses a 12-entry curated alias: HTTP runs real ffuf with numeric
addresses and redirects disabled; HTTPS uses the pinned httpx adapter to preserve
TLS certificate verification. No raw flags, arbitrary wordlists or login guessing.

## SSH profiles

Create `~/.config/blackbox/ssh_profiles.yaml` with mode **0600**. This is a local
configuration file, never a model-written tool argument. Example:

```yaml
lab:
  host: 10.48.158.17
  port: 22
  username: student
  # Omit key_path to use an existing SSH_AUTH_SOCK agent.
  key_path: /home/YOUR_USER/.ssh/lab_ed25519
  known_hosts: /home/YOUR_USER/.ssh/known_hosts
  allowed_read_roots:
    - /home
```

```bash
chmod 600 ~/.config/blackbox/ssh_profiles.yaml
chmod 600 ~/.ssh/lab_ed25519
```

Verify the server fingerprint through your existing trusted SSH workflow first.
BlackBox never turns off host-key validation. Keys are referenced, never pasted
into chat/config, and password authentication is unavailable. Encrypted keys should
be loaded into an agent. Profiles expose only name/target/port/read roots to the
model, not username/key paths. Matching session scope, port and read roots are also
required. Reads use SFTP only; servers without SFTP are reported as blocked.
Canonical path checks, hidden/secret store exclusions, regular-file checks and
byte/depth/entry caps apply. No remote shell, sudo, writes or downloads of home trees.

## Configuration and development

Default private data: `~/.local/share/blackbox` (0700), DB/images/artifacts (0600).
Preferences: `~/.config/blackbox/config.yaml`; see `config.example.yaml` for the
actual flat schema. Settings writes this file atomically. Environment overrides:
`BLACKBOX_DATA_DIR`, `BLACKBOX_CONFIG_DIR`, `BLACKBOX_OLLAMA_URL` (numeric loopback
HTTP only). `.env.example` documents them; `.env` is not loaded automatically.
Do not run multiple servers against the same data directory. The normal CLI holds
an exclusive process lock; doctor checks do not interrupt running sessions.

```bash
./scripts/dev.sh
```

The dev script runs Vite's watched static build and FastAPI reload on the same
loopback origin. Refresh after a UI rebuild; reopen the printed pairing link after
a backend reload. This intentionally preserves the production CSP and origin checks.

```bash
.venv/bin/python -m pytest -q
npm --prefix frontend run typecheck
npm --prefix frontend run build
(cd frontend && npm run test:e2e)
./scripts/smoke_test.sh
```

Browser tests use `/usr/bin/chromium`; set `BLACKBOX_CHROMIUM=/path/to/chromium` to
select another installed binary. They start a temporary local mock Ollama-backed
BlackBox on port 8876; no external targets are contacted. Tests cover real localhost
Nmap/ping/ffuf and a public-key SSH/SFTP fixture with a planted flag. Host binary
tests skip explicitly if the required binary is absent. Synthetic clipboard events
exercise PNG delivery; physical Wayland clipboard behavior still needs a manual check.

Run only the deterministic SSH demo:

```bash
.venv/bin/python -m pytest -v tests/integration/test_ssh_fixture.py::test_agent_retrieves_real_sftp_flag
```

It creates an ephemeral localhost SSH server, temporary key/known_hosts, a scoped
`/home/lab/user.txt`, and a model protocol fixture that chains probe → find → read.
The flag must be read from the actual fixture file and reach the model observation.
Everything is cleaned up. For an opt-in real installed model smoke (host preflight
and synthetic image only):

```bash
.venv/bin/python scripts/real_model_smoke.py huihui_ai/qwen3.5-abliterated:4b
```

## Troubleshooting and limits

- **Ollama unavailable:** start Ollama, check `curl -fsS http://127.0.0.1:11434/api/tags`,
  then Retry connection. `doctor` prints binaries, models, permissions and port status.
- **Default model absent:** choose an installed model; use Settings to probe custom
  models. Download `qwen3.5:4b` only if desired.
- **401 after restart:** reopen the new one-use pairing link from the terminal.
- **Scope denied:** update scope directly; no automatic host hopping or redirect bypass.
- **SSH needs access:** configure a trusted profile/key/agent and known_hosts. Fingerprint
  mismatch requires independent verification. No passwords or fallback exploitation.
- **Tool missing:** install the reported binary and restart to refresh executable paths.
- **Timeout/budget:** partial evidence stays in the journal. Review it before retrying.

Final model answers arrive as a complete block (tool/status/output events stream
live). Real SSH was verified with an in-process SSH/SFTP server, not your external
lab credentials. Physical clipboard capture, remote VPN routing and 50-turn GPU/RAM
soak testing remain manual. See `docs/security.md`, `docs/tools.md`, `docs/design.md`
and `docs/implementation-report.md` for enforced controls and actual verification.
