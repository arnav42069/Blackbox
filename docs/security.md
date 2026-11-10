# Security boundary

BlackBox runs as your normal user on numeric loopback only. It is intended for
explicitly authorized labs. Model output, images, websites and remote files are
untrusted. The model never edits scope, SSH profiles, preferences or tool schemas.

- API bearer auth and same-origin checks protect all sensitive endpoints and SSE.
  Browser GETs omit Origin by browser design; the SPA supplies X-BlackBox-Origin.
  An actual foreign Origin is always denied, even with a matching custom header.
  No permissive CORS policy exists. Host is limited to the configured 127.0.0.1 port.
  CSP blocks remote scripts/images, inline scripts, frames and object embeds.
- One-use, 120-second pairing fragments exchange for a per-process bearer. API
  tokens stay in sessionStorage. No credential material goes into model context.
- Direct attestation creates session scope. IP/CIDR parsing, all-address DNS checks,
  DNS pinning, port validation, TTL, and redirect revalidation run in code. DNS-only
  authorizations pin the approved addresses; later changes require new attestation.
  Multicast, unspecified addresses and the common cloud metadata endpoint are denied.
- Target tools are serialized across sessions. Tool calls, model turns, wall time,
  per-tool time, output and HTTP request counts are bounded. Manual approval cannot
  relax policy. Nmap uses numeric endpoints, no DNS, TCP connect and fixed options.
- Only known discovered binaries run through argv, with an isolated environment and
  new process group. Timeout/Stop/output caps kill the group. ffuf uses a generated
  curated list, isolated HOME/XDG config, pinned HTTP host, fixed rate/concurrency,
  no redirects, no recursion, and ignores response bodies. HTTPS discovery uses
  httpx so certificate verification stays enabled. No arbitrary command/raw flags.
- Workspace files are confined by canonical paths. Hidden files, key/secret stores
  and traversal are rejected. Artifacts are new files under one session directory.
- SSH is SFTP only, public key/agent only, with explicit known_hosts verification.
  SSH config/proxies/tunnels/GSS/forwarding are disabled. Numeric endpoints are
  pinned while host-key identity uses the approved profile hostname. Session and
  profile roots must both permit canonical paths; secret stores and nonregular
  reads are denied. Max depth 5, 500 inspected entries, 100 results, 16 KiB per read.
- Images are Pillow-sniffed, limited to still PNG/JPEG/WebP, 8 MiB and 4096 px/side,
  then normalized to 1600 px with metadata removed. Uploads are transactionally
  associated; failures remove files. Attachments require session-specific auth.
- Markdown is sanitized by DOMPurify. Remote image tags, forms, inline styles and
  active elements are removed. Output is escaped, ANSI/control sequences stripped,
  and common credential/private-key patterns redacted. Human output never includes
  image base64, SSH key paths, private key bodies or pairing bearer tokens.
- Data dirs are 0700; DB, WAL-related files, attachments, reports/preferences are
  0600. Startup marks unfinished runs/tools interrupted, revokes scopes and prunes
  expired data. Normal CLI prevents concurrent servers sharing a data directory.

## Limits

The browser, local OS user, configured SSH keys and SSH server implementation are
trusted. This is not an OS sandbox or protection against another process already
running as your user. A malicious remote SFTP server can lie about filesystem
metadata; canonical checks cannot prove the identity of an untrusted server's
filesystem. Remote responses can contain sensitive text that heuristic redaction
does not recognize. Do not authorize unrelated home trees or put secrets in chat.

A system prompt cannot guarantee model truthfulness. The UI labels model prose
separately from verified observations and retains tool IDs/results. Always check
evidence for consequential claims. Tools cannot add authorization even when remote
content or a model asks them to. No auth attacks, exploit executor, writes to remote
hosts, shell, sudo, arbitrary host reads or credential guessing are registered.


## Planning, screenshots and cached observations

Intent classification and vision results cannot grant scope. The executor validates
registered schemas, action policy, current destination/port authorization and
configured discovery limits before reuse or execution. The policy layer separately
rejects SSH for HTTP/scan/image intents and for tasks without a remote-access
requirement. SSH remains read-only SFTP; remote shell execution is unavailable.

A screenshot is a model transcription, not proof of a live service or scan time.
Literal text, quoted findings, uncertainty and inferred service-to-URL mappings are
stored separately from executed scan evidence. Imported/legacy outputs likewise
remain explicitly unverified. Both may supply candidate endpoints only inside
current approved scope. Scan TTL refers to observation time (upload time for an
image with unknown original scan time). Visual accuracy is model-dependent; the
regression fixture mocks transcription and cannot establish real-model OCR accuracy.

Only fresh complete successful executed scans satisfy scan-cache requests. Scope
changes, session changes, TTL expiry and explicit rescan prevent hits. A later
failed/incomplete equivalent attempt invalidates prior success. Reuse emits a
visible trace and retains the original timestamp/evidence ID. Storage is bounded
to 1000 structured records per session; session deletion/retention cascades to them.
