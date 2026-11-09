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
| net.scan_ports | target, ports="1-1000", service_detection=false | `nmap -sT -Pn -n`, fixed rate 20/s, 1000-port cap, 35 s; active-scan scope |
| web.inspect | url, method="GET" or "HEAD" | httpx, pinned numeric destination/Host/SNI, TLS verification, 3 redirects, 32 KiB body, scoped ports |
| web.discover_paths | url, wordlist="small", rate=2, concurrency=1 | Curated 12 paths, rate ≤5/s, threads ≤2, 45 s ffuf process; active-scan scope and request budget |
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
truncated and denied_reason. Raw output is capped at 64 KiB/tool; observations sent
to the model are compact JSON, ~12 KiB/tool and 40 KiB/run maximum. Output is
untrusted data, not a source of privileges. Logs collapse/expand and can be copied.

`echo_test` exists only as a capability-probe schema; it cannot execute a host
command and is not part of production task tools. Missing binaries produce actual
errors; no executor substitutes mock output. Protocol fixtures live under tests.
