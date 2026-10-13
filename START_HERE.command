#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf '%s\n' '此入口只支持 macOS，没有修改登录自启。' >&2
  exit 2
fi

TASK_PYTHON=''
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  TASK_PYTHON="$PROJECT_ROOT/.venv/bin/python"
else
  # Finder's PATH may omit Homebrew/Python.org, and PATH can contain an Intel
  # interpreter. Check the architecture of the running Python, not the shell.
  for candidate in python3.12 /opt/homebrew/bin/python3.12 /opt/homebrew/opt/python@3.12/bin/python3.12 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 \
                   python3.11 /opt/homebrew/bin/python3.11 /opt/homebrew/opt/python@3.11/bin/python3.11 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
                   python3.10 /opt/homebrew/bin/python3.10 /opt/homebrew/opt/python@3.10/bin/python3.10 /Library/Frameworks/Python.framework/Versions/3.10/bin/python3.10 \
                   python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import platform, sys; sys.exit(0 if sys.platform == "darwin" and platform.machine() == "arm64" and (3, 10) <= sys.version_info[:2] < (3, 13) else 1)' >/dev/null 2>&1; then
      TASK_PYTHON="$(command -v "$candidate")"
      break
    fi
  done
fi

if [[ -z "$TASK_PYTHON" ]]; then
  printf '%s\n' 'Apple 芯片版需要原生 arm64 Python 3.12（支持 3.10–3.12）。请安装 Apple 芯片/universal2 版本，并在未使用 Rosetta 的终端中重新运行。' '安装说明：https://www.python.org/downloads/macos/' >&2
  exit 2
fi

exec "$TASK_PYTHON" "$PROJECT_ROOT/scripts/macos.py" "$@"
