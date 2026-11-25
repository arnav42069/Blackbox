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
# Use the same registered, single-instance backend as production. Vite continues
# watching UI assets; use blackbox restart after Python changes.
.venv/bin/python -m blackbox "$@"
