# shellcheck shell=bash
# Shared helpers for the Linux modules. Sourced, not run.
#
# Every module runs ON the test bed host as root. The work folder is the
# parent of the modules folder (default /opt/hqtb). Secrets come from
# <work>/secrets.env (KEY=VALUE lines, mode 600), written by tb.py or by hand.
set -euo pipefail

TB_MODULES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TB_WORK="${TB_WORK:-$(dirname "$TB_MODULES")}"
TB_SECRETS="${TB_SECRETS:-$TB_WORK/secrets.env}"
TB_NAME="$(basename "${0%.sh}")"

log()  { echo "[$TB_NAME] $*"; }
warn() { echo "[$TB_NAME] WARN: $*" >&2; }
die()  { echo "[$TB_NAME] ERROR: $*" >&2; exit 1; }

# One machine-readable line for tb.py: TBRESULT {"key": ...}
result() { echo "TBRESULT $*"; }

need_root() { [[ $EUID -eq 0 ]] || die "run as root"; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

# Load secrets and export the Firebird client variables. isql, gfix, gstat and
# nbackup read ISC_USER / ISC_PASSWORD, so the password never shows in `ps`.
load_secrets() {
  if [[ -f "$TB_SECRETS" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$TB_SECRETS"
    set +a
  fi
  export ISC_USER="${TB_FB_USER:-SYSDBA}"
  [[ -n "${TB_FB_PASSWORD:-}" ]] || die "TB_FB_PASSWORD is not set (secrets file $TB_SECRETS)"
  export ISC_PASSWORD="$TB_FB_PASSWORD"
}

# json_get FILE KEY.PATH -> prints the value ('' when absent).
json_get() {
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    v = json.load(open(sys.argv[1], encoding="utf-8"))
    for k in sys.argv[2].split("."):
        v = v[int(k)] if isinstance(v, list) else v.get(k)
        if v is None:
            break
    print("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else v))
except FileNotFoundError:
    pass
PY
}

# Find the Firebird systemd unit: firebird.service first, else the only unit
# whose name has "firebird" in it (fbagent units excluded).
detect_fb_unit() {
  local units u
  mapfile -t units < <(systemctl list-unit-files --type=service --no-legend 2>/dev/null \
    | awk '{print $1}' | grep -Ei 'firebird' | grep -vi fbagent || true)
  for u in "${units[@]}"; do
    [[ "$u" == "firebird.service" ]] && { echo firebird; return; }
  done
  if [[ ${#units[@]} -eq 1 ]]; then echo "${units[0]%.service}"; return; fi
  echo ""
}

# fb_conf_port ROOT -> RemoteServicePort from firebird.conf; '' when it is
# not set (Firebird then listens on 3050).
fb_conf_port() {
  [[ -f "$1/firebird.conf" ]] || return 0
  sed -nE 's/^[[:space:]]*RemoteServicePort[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' "$1/firebird.conf" | tail -n1
}

fb_tool() { # fb_tool ROOT NAME
  if [[ -x "$1/bin/$2" ]]; then echo "$1/bin/$2"; else echo "$1/$2"; fi
}

# wait_until SECONDS CMD... : run CMD every 2 s until it succeeds.
wait_until() {
  local limit="$1"; shift
  local end=$((SECONDS + limit))
  while (( SECONDS < end )); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

unit_exists() { systemctl list-unit-files "$1.service" --no-legend 2>/dev/null | grep -q "^$1.service"; }

# Arguments: every module takes --key value pairs. parse_args sets ARG_<key>
# (dashes become underscores). Flags without a value are not supported:
# pass "--flag true".
parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --*)
        local k="${1#--}"; k="${k//-/_}"
        [[ $# -ge 2 ]] || die "missing value for $1"
        printf -v "ARG_$k" '%s' "$2"
        shift 2 ;;
      *) die "unexpected argument: $1" ;;
    esac
  done
}

arg() { # arg NAME [DEFAULT]
  local v="ARG_$1"
  if [[ -n "${!v:-}" ]]; then echo "${!v}"; else echo "${2:-}"; fi
}

has() { [[ ",$1," == *",$2,"* ]]; }
