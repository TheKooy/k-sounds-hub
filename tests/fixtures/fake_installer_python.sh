#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  target="${3:?missing fake venv target}"
  mkdir -p "$target/bin"
  cp -- "$0" "$target/bin/python"
  chmod +x "$target/bin/python"
  exit 0
fi

if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
  if [[ "${KSH_FAKE_PYTHON_FAIL_INSTALL:-0}" == "1" ]]; then
    exit 42
  fi
  exit 0
fi

if [[ "${1:-}" == "-c" ]]; then
  printf '%s\n' '/fake/python 3.13.0'
  exit 0
fi

if [[ "${1:-}" == "-" ]]; then
  stdin_script="$(cat)"

  # install.sh uses `python -` to verify Python >= 3.11.
  if [[ "$stdin_script" == *"sys.version_info"* ]]; then
    exit 0
  fi

  # start_ksound_hub_v2.sh uses `python -` for its IPC restore probe.
  # The isolated lifecycle fixture has no existing Glass IPC server, so this
  # probe must fail and let the launcher continue to the real frontend command.
  if [[ "$stdin_script" == *"KSH_IPC_SOCKET_PATH"* || "$stdin_script" == *"socket.socket"* ]]; then
    exit 1
  fi

  exit 0
fi

if [[ "${1:-}" == "-m" ]]; then
  if [[ -n "${KSH_FAKE_PYTHON_LOG:-}" ]]; then
    printf '%s\n' "${2:-}" >> "$KSH_FAKE_PYTHON_LOG"
  fi
  exit 0
fi

exit 0
