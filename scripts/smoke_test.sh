#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
.venv/bin/python -m pytest -q
npm --prefix frontend run build
(cd frontend && npm run test:e2e)
