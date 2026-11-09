#!/usr/bin/env bash
# Cooperative writer lease for interactive Codex, editors, or other automation.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$root/.runtime"
if (( $# == 0 )); then set -- codex; fi
exec flock -n -E 75 "$root/.runtime/qa-writer.lock" "$@"
