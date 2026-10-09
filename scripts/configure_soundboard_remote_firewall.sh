#!/usr/bin/env bash
set -Eeuo pipefail

HTTP_PORT="${KSOUND_SOUNDBOARD_WEB_PORT:-8765}"
DISCOVERY_PORT="${KSOUND_SOUNDBOARD_DISCOVERY_PORT:-8766}"
REQUESTED_INTERFACE="${KSH_REMOTE_INTERFACE:-}"
REQUESTED_ZONE="${KSH_REMOTE_FIREWALL_ZONE:-}"
CHECK_ONLY=0

usage() {
  echo "Usage: $0 [--check] [--interface NAME] [--zone NAME]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      CHECK_ONLY=1
      shift
      ;;
    --interface)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      REQUESTED_INTERFACE="$2"
      shift 2
      ;;
    --zone)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      REQUESTED_ZONE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

command -v firewall-cmd >/dev/null 2>&1 || {
  echo "firewalld is not installed; no firewalld rule is required."
  exit 0
}

if [[ "$(firewall-cmd --state 2>/dev/null || true)" != "running" ]]; then
  echo "firewalld is not running; no firewalld rule is required."
  exit 0
fi

if [[ -z "$REQUESTED_INTERFACE" ]]; then
  REQUESTED_INTERFACE="$(
    ip -4 route show default 2>/dev/null \
      | awk '/[[:space:]]dev[[:space:]]/ { for (i = 1; i <= NF; i++) if ($i == "dev" && i < NF) { print $(i + 1); exit } }'
  )"
fi

if [[ -z "$REQUESTED_ZONE" && -n "$REQUESTED_INTERFACE" ]]; then
  REQUESTED_ZONE="$(firewall-cmd --get-zone-of-interface="$REQUESTED_INTERFACE" 2>/dev/null || true)"
  [[ "$REQUESTED_ZONE" != "no zone" ]] || REQUESTED_ZONE=""
fi

if [[ -z "$REQUESTED_ZONE" ]]; then
  REQUESTED_ZONE="$(firewall-cmd --get-default-zone)"
fi

[[ -n "$REQUESTED_ZONE" ]] || {
  echo "Could not resolve the active firewalld zone." >&2
  exit 1
}

RULES=("${HTTP_PORT}/tcp" "${DISCOVERY_PORT}/udp")

ZONE_TARGET="$(firewall-cmd --zone="$REQUESTED_ZONE" --get-target 2>/dev/null || true)"
if [[ "${ZONE_TARGET^^}" == "ACCEPT" ]]; then
  echo "OK: firewalld zone $REQUESTED_ZONE accepts incoming traffic."
  exit 0
fi

missing=0
for rule in "${RULES[@]}"; do
  runtime_allowed=0
  permanent_allowed=0
  firewall-cmd --zone="$REQUESTED_ZONE" --query-port="$rule" >/dev/null 2>&1 \
    && runtime_allowed=1
  firewall-cmd --zone="$REQUESTED_ZONE" --permanent --query-port="$rule" >/dev/null 2>&1 \
    && permanent_allowed=1

  if [[ $runtime_allowed -eq 1 && $permanent_allowed -eq 1 ]]; then
    echo "OK: firewalld zone $REQUESTED_ZONE allows $rule (runtime and permanent)"
  else
    states=()
    [[ $runtime_allowed -eq 1 ]] || states+=("runtime")
    [[ $permanent_allowed -eq 1 ]] || states+=("permanent")
    echo "MISSING: firewalld zone $REQUESTED_ZONE lacks $rule (${states[*]})"
    missing=1
  fi
done

if [[ $CHECK_ONLY -eq 1 ]]; then
  exit "$missing"
fi

if [[ $missing -eq 0 ]]; then
  echo "Remote firewall configuration is already correct."
  exit 0
fi

for rule in "${RULES[@]}"; do
  firewall-cmd --zone="$REQUESTED_ZONE" --add-port="$rule"
  firewall-cmd --zone="$REQUESTED_ZONE" --permanent --add-port="$rule"
done

for rule in "${RULES[@]}"; do
  firewall-cmd --zone="$REQUESTED_ZONE" --query-port="$rule" >/dev/null
  firewall-cmd --zone="$REQUESTED_ZONE" --permanent --query-port="$rule" >/dev/null
done

echo "Remote firewall configured for interface ${REQUESTED_INTERFACE:-unknown}, zone $REQUESTED_ZONE."
