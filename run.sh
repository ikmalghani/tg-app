#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export PYENV_ROOT="${PYENV_ROOT:-${HOME}/.pyenv}"
export PATH="${PYENV_ROOT}/bin:${PATH}"

if command -v pyenv >/dev/null 2>&1; then
  eval "$(pyenv init -)"
fi

PYTHON=""
for candidate in \
  "${PYENV_ROOT}/versions/tgappenv/bin/python3" \
  "${PYENV_ROOT}/versions/3.12.13/bin/python3"
do
  if [[ -x "${candidate}" ]]; then
    PYTHON="${candidate}"
    break
  fi
done

if [[ -z "${PYTHON}" ]]; then
  echo "Could not find pyenv Python (tgappenv or 3.12.13)." >&2
  echo "Install with: pyenv install 3.12.13 && pyenv virtualenv 3.12.13 tgappenv" >&2
  exit 1
fi

exec "${PYTHON}" "${ROOT_DIR}/tg-app.py"
