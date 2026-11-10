# Implemented tools

All arguments use strict Pydantic JSON schemas, rejecting unknown fields. They are
advertised through Ollama `tools` only when the model has supported capability and
required binaries are present. All attempts and denials enter the persisted ledger. Subprocess tools also emit a
`tool_command` event with exact argv (displayed under Executed argv).
Default budgets: 20 calls, 12 model turns, 300 seconds; sequential target tools.

| Name | Arguments | Execution / required access |
|---|---|---|
| host.preflight | none | Real OS/binary/agent detection; no target required |
| workspace.list | path="." | At most 200 safe workspace entries |
| workspace.read | path, max_bytes=8192 | Regular workspace file, max 32 KiB |
| workspace.search | path=".", pattern, cap=30 | Literal text, depth 5, 200 files, 100 matches |
| net.ping | target | `ping -n -c 2 -W 2`, 8 s, explicit scope |
| net.scan_ports | target, ports="1-1000", service_detection=false, rescan=false | `nmap -sT -Pn -n`, fixed rate 20/s, 1000-port cap, 35 s; active-scan scope |
| web.inspect | url, method="GET" or "HEAD" | Pinned httpx GET (3 redirects, 32 KiB body) or curl HEAD (no redirects); verified TLS and scoped ports |
| web.discover_paths | url, wordlist="small", rate=2, concurrency=1, timeout_seconds=40, rescan=false | Curated 12 paths, rate ≤ configured limit (hard max 5/s), threads ≤ configured limit (hard max 2), runtime ≤ configured limit (hard max 40 s plus process termination grace); active-scan scope and request budget |
| web.technologies | url | WhatWeb aggression 1 on pinned HTTP, redirects disabled, 15 s; HTTPS unsupported (use verified headers instead) |
| web.tls | url | OpenSSL handshake/certificate validation on a pinned HTTPS endpoint, 12 s; not an exhaustive cipher audit |
| scan.lookup | target optional | Internal session/scope-aware retrieval; explicit timestamps, source, status, completeness and freshness; no scan |
| scan.parse | path, format, observed_at optional | Existing workspace export ≤64 KiB; format nmap_xml / ffuf_json / terminal_text; unknown time is stale, future times rejected; imported source remains unverified |
| ssh.probe | target, profile | Existing public-key SSH/SFTP access; trusted known_hosts |
| ssh.list_dir | target, profile, path="/home" | Canonical approved directories, 100 entries |
| ssh.find_files | target, profile, path="/home", patterns, max_depth=3, max_results=30 | Read-only SFTP traversal; max depth 5, results 100, inspected entries 500 |
| ssh.read_file | target, profile, path, max_bytes=4096 | Regular approved file, maximum 16 KiB |
| artifacts.save | filename, text | New .txt/.md/.json session artifact, max 32000 characters |

Target parameters are single IP/hostnames, never shell strings or host expressions.
Ports accept only validated comma-separated numbers/ranges. Requests reserve the
actual curated wordlist budget before ffuf launch. HTTP discovery does not follow
redirects or read response bodies. HTTPS discovery uses the HTTP adapter with
certificate verification instead of ffuf's permissive TLS transport. No uncurated
wordlist, POST payload, cookies, proxies, extensions or model-supplied flags exist.

ToolResult includes ok, exit_code, stdout/stderr, summary, evidence, elapsed_ms,
truncated, denied_reason and cached_scan_id. Raw output is capped at 64 KiB/tool; observations sent
to the model are compact JSON, ~12 KiB/tool and 40 KiB/run maximum. Output is
untrusted data, not a source of privileges. Logs collapse/expand and can be copied.

`echo_test` exists only as a capability-probe schema; it cannot execute a host
command and is not part of production task tools. Missing binaries produce actual
errors; no executor substitutes mock output. Protocol fixtures live under tests.

`vision.observe` is a controller stage for attached images, not a model-callable
network tool. Metadata and probes, never a model name, determine vision support.
A selected model without vision needs an installed, capable `vision_model` fallback
or the upload is rejected before creating a message.

Preferences: `scan_freshness_seconds=900`, `http_rate=2`, `http_concurrency=1`,
`discovery_timeout_seconds=40`, `vision_model=null`. Settings expose the fallback,
freshness, rate and timeout. Natural-language “rescan”, “scan again”, “fresh scan”
and “ignore cache” bypass executed scan reuse. No scanner accepts arbitrary flags.
SQL injection intent is recognized but blocked under the current PRD action policy;
sqlmap and arbitrary remote commands are not registered.


Dependency handling: `iputils`, `nmap`, `openssh` are baseline requirements for the
full supported toolset. ffuf is preferred, with native bounded HTTP discovery when
missing; HTTPS always retains certificate-verifying native discovery. WhatWeb is
optional and missing fingerprinting falls back to explicitly limited HTTP headers.
Native `grim`/`wl-clipboard` are optional and do not gate browser screenshot uploads.
Startup capability details are exposed in `/api/preflight`, doctor, and UI details.
