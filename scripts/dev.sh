#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
npm --prefix frontend run build
(cd frontend && npm exec -- vite build --watch) &
ui_pid=$!
cleanup() { kill "$ui_pid" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
# A single origin preserves production Host/Origin checks and CSP. Reload browser after UI rebuild.
.venv/bin/python -m uvicorn scripts.dev_server:factory --factory --host 127.0.0.1 --port 8765 --reload --reload-dir backend --reload-dir scripts
