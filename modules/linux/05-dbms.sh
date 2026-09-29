#!/usr/bin/env bash
# Module 05 (Linux): prepare a fresh host and install HQbird/Firebird.
#
#   05-dbms.sh install --installer-url-b64 B64 [--root-b64 B64]
#   05-dbms.sh check   [--root-b64 B64]
#
# Why the odd names and base64: the Firebird installer refuses to run when
# any process has "firebird" in its command line ("An instance of the
# Firebird server seems to be running"). This module's own command line must
# not contain the word, so the file is not named after it and the installer
# URL and the server root (default /opt/firebird) arrive base64-encoded.
#
# install:
#   - apt packages the test bed needs (python3, curl, chrony, openssl);
#   - HQbird/Firebird from the installer script at --installer-url, unless
#     <fb-root>/bin/isql already exists;
#   - sets the SYSDBA password to TB_FB_PASSWORD when the installer left
#     TB_FB_INITIAL_PASSWORD (secrets file) in place;
#   - turns off HQbird DataGuard jobs, so they do not touch test databases;
#   - enables the Firebird systemd unit.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
parse_args "$@"
need_root
b64d() { base64 -d <<<"$1" 2>/dev/null || die "bad base64 argument"; }
FB_ROOT="/opt/firebird"; [[ -z "$(arg root_b64)" ]] || FB_ROOT="$(b64d "$(arg root_b64)")"
export DEBIAN_FRONTEND=noninteractive

wait_apt() {
  local i=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1 \
     || fuser /var/lib/dpkg/lock >/dev/null 2>&1; do
    i=$((i + 1)); (( i < 180 )) || die "apt lock timeout"
    sleep 5
  done
}

isql_ok() { # PASSWORD
  printf 'select 1 from rdb$database;\n' | ISC_USER=SYSDBA ISC_PASSWORD="$1" \
    "$(fb_tool "$FB_ROOT" isql)" -q "localhost:$FB_ROOT/examples/empbuild/employee.fdb" >/dev/null 2>&1
}

case "$CMD" in
  install)
    load_secrets
    URL=""; [[ -z "$(arg installer_url_b64)" ]] || URL="$(b64d "$(arg installer_url_b64)")"
    log "apt packages"
    wait_apt; apt-get update -y >/dev/null
    wait_apt; apt-get install -y --no-install-recommends chrony curl ca-certificates python3 openssl psmisc >/dev/null
    systemctl enable --now chrony >/dev/null 2>&1 || true
    if [[ ! -x "$(fb_tool "$FB_ROOT" isql)" ]]; then
      [[ "$URL" == https://* ]] || die "--installer-url-b64 is required (an https URL)"
      log "install HQbird/Firebird (takes a few minutes)"
      curl -fsSL -o /tmp/tb-fb-installer.sh "$URL"
      bash /tmp/tb-fb-installer.sh >/tmp/tb-fb-installer.log 2>&1 || { tail -n 40 /tmp/tb-fb-installer.log >&2; die "installer failed"; }
      rm -f /tmp/tb-fb-installer.sh
    else
      log "Firebird already in $FB_ROOT"
    fi
    UNIT="$(detect_fb_unit)"; [[ -n "$UNIT" ]] || die "no Firebird systemd unit after install"
    systemctl enable --now "$UNIT" >/dev/null 2>&1 || true
    # Fresh HQbird 2.5/3.0: the server stops at its first start ("Replication
    # server initialization error", "Valid date is expired!") while DataGuard
    # has not written its replconf file yet, and systemd does not start it
    # again. Start it again until one of the passwords logs in.
    login_ok() { isql_ok "$TB_FB_PASSWORD" || { [[ -n "${TB_FB_INITIAL_PASSWORD:-}" ]] && isql_ok "$TB_FB_INITIAL_PASSWORD"; }; }
    for try in 1 2 3 4 5 6; do
      wait_until 20 login_ok && break
      log "Firebird does not answer (try $try): start $UNIT again"
      systemctl restart "$UNIT" >/dev/null 2>&1 || true
      sleep 10
    done
    if ! isql_ok "$TB_FB_PASSWORD"; then
      [[ -n "${TB_FB_INITIAL_PASSWORD:-}" ]] || die "SYSDBA login failed and TB_FB_INITIAL_PASSWORD is not set"
      isql_ok "$TB_FB_INITIAL_PASSWORD" || die "SYSDBA login fails with both passwords"
      log "set the SYSDBA password from the secrets file"
      pw="${TB_FB_PASSWORD//\'/\'\'}"
      printf "alter user SYSDBA password '%s';\ncommit;\n" "$pw" | ISC_USER=SYSDBA ISC_PASSWORD="$TB_FB_INITIAL_PASSWORD" \
        "$(fb_tool "$FB_ROOT" isql)" -q "localhost:$FB_ROOT/examples/empbuild/employee.fdb" >/dev/null
      isql_ok "$TB_FB_PASSWORD" || die "SYSDBA password change did not work"
    fi
    if [[ -d /opt/hqbird/conf ]]; then
      log "turn off HQbird DataGuard jobs"
      find /opt/hqbird/conf -name 'job.properties' -exec sed -i 's/^job.enabled=.*/job.enabled=false/' {} + 2>/dev/null || true
      find /opt/hqbird/conf -name 'database.properties' \
        -exec sed -i 's/^db.replication_role=.*/db.replication_role=switchedoff/' {} + 2>/dev/null || true
    fi
    result "{\"fb_unit\":\"$UNIT\",\"isql\":true}"
    ;;

  check)
    load_secrets
    ok=false; isql_ok "$TB_FB_PASSWORD" && ok=true
    result "{\"fb_unit\":\"$(detect_fb_unit)\",\"login\":$ok}"
    [[ "$ok" == true ]]
    ;;

  *) die "usage: 05-dbms.sh install|check [--key value ...]" ;;
esac
