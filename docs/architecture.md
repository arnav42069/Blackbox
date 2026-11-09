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
