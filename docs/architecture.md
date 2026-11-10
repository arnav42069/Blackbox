# Architecture

A single loopback FastAPI process serves a built Preact SPA. The browser pairs once
using an expiring URL fragment, then uses a bearer header and explicit same-origin
header for API calls and fetch-based SSE. Only the backend talks to Ollama.

SQLite stores sessions, full structured chat/tool messages, private attachment
references, run state, tool records, and a monotonic replayable event journal. A
serialized connection makes short transactions; no transaction spans external IO.
Startup marks active runs interrupted and revokes scope attestation. No commands
resume automatically. Each session permits one run; target tools are sequential.

Model requests pass typed schemas, policy and scope validation before execution.
Subprocesses use fixed binary paths and argv in their own process group. HTTP and
SSH connections pin validated numeric destinations. SSH uses SFTP only: no remote
shell exists. Read paths are constrained and resolved to prevent symlink escapes.

The implementation follows the PRD's five gates. Tests use isolated data/config
roots and explicit protocol fixtures; production code never substitutes mock tools.

## Planning and evidence reuse

The existing agent now runs this deterministic stage before requesting model tool
calls:

`instruction → intent → scoped evidence retrieval → target/protocol resolution → typed plan → policy → executor → parser → SQLite evidence → grounded response`

`core/planner.py` defines `ExecutionPlan`, including intent, target, authorization
scope, relevant previous findings, selected tool, rationale, schema-validated
arguments, constraints and expected output. Straightforward port scans, headers,
HTTP discovery and stored-result analysis finish without a planning or answer LLM
call. Other tasks retain the existing tool loop and completion review. Plans and
image observations are inspectable in the existing Activity panel and SSE journal.

`core/vision.py` sends current attachments to the selected verified vision model,
or the explicitly configured installed `vision_model`. Text-only models never
receive image bytes. Strict JSON extraction preserves literal visible text and
uncertainties; deterministic parsers derive quoted findings. Service-derived URLs
are labelled as inferred candidates. Diagrams provide labels, not invented links.
Image content remains untrusted input, never scope or policy instructions. The
ordinary planning context receives these structured observations and relevant scan
records, rather than rereading old images on each continuation.

`core/scans.py` extends SQLite with session-owned `scan_results`. Records include
logical target, tool, normalized arguments, observation/storage timestamps,
execution status, completeness, findings, source and evidence ID. Parsers support
Nmap XML, ffuf export/JSON lines, terminal Nmap text, literal URLs, IPs, address/port
labels, HTTP codes and error lines. Nmap retains readable stdout and produces a
separate bounded XML file. ffuf destinations are restored to the authorized
logical URL before evidence storage.

Cache identity includes session, exact authorization record and equivalent typed
arguments (canonical ports and URL authority). The executor revalidates scope and
policy even on hits. Only successful, complete executed scans within the configured
TTL can satisfy equivalent scan requests. A newer failed/incomplete equivalent
attempt invalidates an older success. `rescan` bypasses lookup. Failed and partial
records remain inspectable. Older raw tool journals are imported lazily with their
original timestamps and explicitly unknown scope binding/completeness; they never
become executable scan-cache entries.

Endpoint resolution uses the explicit target, then the most recently mentioned
conversation target, then matching scoped evidence. Multiple remaining origins
require an exact URL. Screenshot/import data can supply an explicitly unverified
candidate, while actual HTTP results are separately attributed. Stale candidates
and failed executed results are not used for automatic endpoint selection. Scope
and DNS/port validation still precede any execution. There is no cross-session reuse.

HTTP discovery uses the existing curated ffuf adapter on HTTP. HTTPS retains the
existing certificate-verifying native adapter because ffuf skips certificate
verification. Configured rate, concurrency, runtime and scope request budget are
independently enforced. Partial ffuf output cannot be recorded as a successful
complete scan. SSH schemas are excluded for unrelated tasks and the policy layer
rejects SSH when remote access was not requested; IP addresses never imply SSH.
