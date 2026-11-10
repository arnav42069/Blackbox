#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
for prerequisite in python node npm; do
  if ! command -v "$prerequisite" >/dev/null; then
    echo "Missing $prerequisite. On Arch: sudo pacman -S --needed python python-pip nodejs npm" >&2
    exit 1
  fi
done
python -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
if [[ ! -x .venv/bin/python ]]; then
  python -m venv .venv
fi
if command -v uv >/dev/null; then
  uv pip install --python "$project_dir/.venv/bin/python" -r requirements.lock
  uv pip install --python "$project_dir/.venv/bin/python" --no-deps -e .
else
  .venv/bin/python -m ensurepip --upgrade
  .venv/bin/python -m pip install -r requirements.lock
  .venv/bin/python -m pip install --no-deps -e .
fi
npm --prefix frontend ci
npm --prefix frontend run build
mkdir -p "$HOME/.local/bin"
.venv/bin/python - "$project_dir" <<'PY'
import pathlib,shlex,sys
project=pathlib.Path(sys.argv[1])
wrapper=pathlib.Path.home()/'.local/bin/blackbox'
wrapper.write_text('#!/usr/bin/env bash\nexec '+shlex.quote(str(project/'.venv/bin/blackbox'))+' "$@"\n')
wrapper.chmod(0o755)
PY
# Native baseline tools are required for the full supported lab capability set.
for entry in 'ping:iputils' 'nmap:nmap' 'ssh:openssh'; do
  binary="${entry%%:*}"; package="${entry#*:}"
  if ! command -v "$binary" >/dev/null; then
    echo "Missing baseline requirement $package ($binary). Install: sudo pacman -S --needed iputils nmap openssh" >&2
  fi
done
for entry in 'ffuf:ffuf:preferred HTTP discovery (bounded native fallback available)' 'whatweb:whatweb:optional web fingerprinting' 'grim:grim:optional native screenshot capture' 'wl-copy:wl-clipboard:optional native clipboard' 'wl-paste:wl-clipboard:optional native clipboard'; do
  IFS=: read -r binary package role <<< "$entry"
  if ! command -v "$binary" >/dev/null; then
    echo "Missing $package: $role. Install: sudo pacman -S --needed $package (repository availability varies)."
  fi
done
echo 'Installed. Run: ~/.local/bin/blackbox doctor'
echo 'Then: ~/.local/bin/blackbox'
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo 'For this Bash session: export PATH="$HOME/.local/bin:$PATH"' ;;
esac
echo 'Ollama is installed separately. Models are never downloaded automatically.'
