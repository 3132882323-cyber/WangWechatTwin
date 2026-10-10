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
  for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 13) else 1)' >/dev/null 2>&1; then
      TASK_PYTHON="$(command -v "$candidate")"
      break
    fi
  done
fi

if [[ -z "$TASK_PYTHON" ]]; then
  printf '%s\n' '请先安装 Python 3.12（支持 3.10–3.12），然后再次打开此文件。' '安装说明：https://www.python.org/downloads/macos/' >&2
  exit 2
fi

exec "$TASK_PYTHON" "$PROJECT_ROOT/scripts/macos.py" "$@"
