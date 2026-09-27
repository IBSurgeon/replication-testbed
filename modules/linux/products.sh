# shellcheck shell=bash
# Install / remove steps shared by 10-local.sh and 20-goafts.sh. Sourced.
#
# Stage layout (filled by tb.py, or by hand):
#   <stage>/bin/fbagent  <stage>/bin/hqclusternode  <stage>/bin/hqbirdrcm
#   <stage>/conf/node.json  <stage>/conf/rcm.json
#   <stage>/certs/       ca.crt + this node's <node_id>.crt/.key
#   <stage>/rcm-certs/   ca.crt + rcm.crt/.key

# ---------------------------------------------------------------- Firebird --
fb_save_pristine_conf() { # ROOT
  local conf="$1/replication.conf"
  if [[ -f "$conf" && ! -f "$conf.tb-pristine" ]]; then
    cp -p "$conf" "$conf.tb-pristine"
    log "saved pristine $conf"
  fi
}

fb_restore_pristine_conf() { # ROOT UNIT
  local conf="$1/replication.conf"
  if [[ -f "$conf.tb-pristine" ]]; then
    cp -f "$conf.tb-pristine" "$conf"
    rm -f "$conf.tb-pristine"
    log "restored pristine $conf"
    if [[ -n "$2" ]]; then systemctl restart "$2" || warn "restart $2 failed"; fi
  fi
}

# ----------------------------------------------------------------- fbagent --
# Write agent_config.json for local_api only (no goafts enrollment).
fbagent_write_config() { # DIR FB_ROOT FB_PORT FB_UNIT API_PORT INSTANCE SERVICE
  local dir="$1"
  [[ -n "${TB_FBAGENT_TOKEN:-}" ]] || die "TB_FBAGENT_TOKEN is not set (secrets file)"
  FB_ROOT="$2" FB_PORT="$3" FB_UNIT="$4" API_PORT="$5" INSTANCE="$6" SERVICE="$7" DIR="$dir" \
  python3 - <<'PY'
import base64, datetime, json, os, socket
e = os.environ
host = socket.gethostname().split(".")[0].lower()
aid = f"{host}{datetime.datetime.utcnow().strftime('%y%m%d')}"
cfg = {
  "agent": {"id": aid, "max_concurrent_long_tasks": 2},
  "firebird": {
    "credentials": {"username": e.get("TB_FB_USER", "SYSDBA"),
                    "password_encrypted": base64.b64encode(e["TB_FB_PASSWORD"].encode()).decode()},
    "install_path": e["FB_ROOT"], "port": int(e["FB_PORT"]), "version": "auto",
    "restart": {"services": [{"windows_name": "FirebirdServerDefaultInstance",
                              "linux_unit": e["FB_UNIT"]}]},
  },
  "goafts": {
    "agent_id": aid, "server_url": "",
    "cert_path": "certs/agent.crt", "key_path": "certs/agent.key", "ca_path": "certs/ca.crt",
    "outcoming_dir": "outcoming", "journal_path": "logs/sent_files.journal.jsonl",
    "delete_after_send": True,
    "auto_update": {"enabled": False, "systemd_unit": e["SERVICE"], "service_name": "HQbirdFBAgent"},
  },
  "logging": {"file": "logs/firebird-agent.jsonl", "level": "info"},
  "storage": {"data_path": "logs"},
  "log_rotation": {"enabled": True, "logs_path": "logs"},
  "local_api": {"enabled": True, "listen": f"127.0.0.1:{e['API_PORT']}",
                "token": e["TB_FBAGENT_TOKEN"], "instance_id": e["INSTANCE"]},
}
path = os.path.join(e["DIR"], "agent_config.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
os.chmod(path, 0o640)
print("wrote", path)
PY
}

# Turn on local_api in an existing agent_config.json (goafts-enrolled agent).
fbagent_patch_local_api() { # DIR API_PORT INSTANCE
  [[ -n "${TB_FBAGENT_TOKEN:-}" ]] || die "TB_FBAGENT_TOKEN is not set (secrets file)"
  DIR="$1" API_PORT="$2" INSTANCE="$3" python3 - <<'PY'
import base64, json, os
e = os.environ
path = os.path.join(e["DIR"], "agent_config.json")
cfg = json.load(open(path, encoding="utf-8"))
cfg["local_api"] = {"enabled": True, "listen": f"127.0.0.1:{e['API_PORT']}",
                    "token": e["TB_FBAGENT_TOKEN"], "instance_id": e["INSTANCE"]}
fb = cfg.setdefault("firebird", {})
cred = fb.setdefault("credentials", {})
cred["username"] = e.get("TB_FB_USER", "SYSDBA")
cred.pop("password", None)
cred["password_encrypted"] = base64.b64encode(e["TB_FB_PASSWORD"].encode()).decode()
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
print("local_api enabled in", path)
PY
}

fbagent_check() { # API_PORT INSTANCE FB_PORT
  local out
  out="$(curl -fsS -H "Authorization: Bearer ${TB_FBAGENT_TOKEN}" \
    "http://127.0.0.1:$1/v1/instances/$2")" || return 1
  PORT="$3" python3 -c '
import json, os, sys
j = json.loads(sys.stdin.read())
p = int(j.get("port") or 0)
assert p == int(os.environ["PORT"]), ("fbagent instance port", p)
print("fbagent instance OK: port", p, "state", j.get("state"))' <<<"$out"
}

fbagent_install_local() { # STAGE DIR FB_ROOT FB_PORT FB_UNIT API_PORT INSTANCE SERVICE
  local stage="$1" dir="$2"
  [[ -f "$stage/bin/fbagent" ]] || die "missing $stage/bin/fbagent"
  log "fbagent -> $dir"
  systemctl stop "$8" 2>/dev/null || true
  mkdir -p "$dir"/{logs,certs,updates,outcoming}
  install -m 0755 "$stage/bin/fbagent" "$dir/fbagent"
  fbagent_write_config "$dir" "$3" "$4" "$5" "$6" "$7" "$8"
  chown -R firebird:firebird "$dir"
  (cd "$dir" && ./fbagent --install)
  systemctl enable --now "$8"
  wait_until 60 fbagent_check "$6" "$7" "$4" || die "fbagent local_api does not answer on 127.0.0.1:$6"
  fbagent_check "$6" "$7" "$4"
}

fbagent_uninstall() { # DIR SERVICE
  local dir="$1"
  if [[ -x "$dir/fbagent" ]]; then
    systemctl stop "$2" 2>/dev/null || true
    (cd "$dir" && ./fbagent --uninstall) || warn "fbagent --uninstall failed"
  fi
  if unit_exists "$2"; then
    systemctl disable --now "$2" 2>/dev/null || true
    rm -f "/etc/systemd/system/$2.service"
  fi
  # hqmonitor is installed by an enrolled fbagent by default.
  for u in /etc/systemd/system/hqbirdmonitor*.service; do
    [[ -e "$u" ]] || continue
    systemctl disable --now "$(basename "$u" .service)" 2>/dev/null || true
    rm -f "$u"
  done
  systemctl daemon-reload
  rm -rf "$dir"
  log "fbagent removed from $dir"
}

# ----------------------------------------------------------- hqclusternode --
node_install() { # STAGE NODE_DIR DB_ROOT SVC_USER
  local stage="$1" dir="$2" dbroot="$3" user="${4:-root}"
  [[ -f "$stage/bin/hqclusternode" ]] || die "missing $stage/bin/hqclusternode"
  [[ -f "$stage/conf/node.json" ]] || die "missing $stage/conf/node.json"
  log "hqclusternode -> $dir"
  mkdir -p "$dir/certs" "$dbroot"
  if [[ -f "$dir/node.json" ]]; then
    "$dir/hqclusternode" svc stop -config "$dir/node.json" 2>/dev/null || true
  fi
  install -m 0755 "$stage/bin/hqclusternode" "$dir/hqclusternode"
  install -m 0640 "$stage/conf/node.json" "$dir/node.json"
  cp -f "$stage/certs/"* "$dir/certs/"
  chown -R root:firebird "$dir"
  chmod 750 "$dir/certs"; chmod 640 "$dir/certs/"*
  chown -R firebird:firebird "$dbroot"
  "$dir/hqclusternode" svc uninstall -config "$dir/node.json" >/dev/null 2>&1 || true
  "$dir/hqclusternode" svc install -config "$dir/node.json" -certs "$dir/certs" \
    -bin "$dir/hqclusternode" -user "$user" -group firebird
  "$dir/hqclusternode" svc start -config "$dir/node.json"
  wait_until 60 "$dir/hqclusternode" healthcheck -config "$dir/node.json" -certs "$dir/certs" \
    || die "node does not answer /v1/health"
  log "node healthy"
}

node_uninstall() { # NODE_DIR
  local dir="$1"
  if [[ -f "$dir/node.json" && -x "$dir/hqclusternode" ]]; then
    "$dir/hqclusternode" svc stop -config "$dir/node.json" 2>/dev/null || true
    "$dir/hqclusternode" svc uninstall -config "$dir/node.json" || warn "svc uninstall failed"
  fi
  for u in /etc/systemd/system/hqclusternode-*-p*.service; do
    [[ -e "$u" ]] || continue
    if grep -q "$dir/" "$u"; then
      systemctl disable --now "$(basename "$u" .service)" 2>/dev/null || true
      rm -f "$u"
    fi
  done
  systemctl daemon-reload
  rm -rf "$dir"
  log "node removed from $dir"
}

# --------------------------------------------------------------- hqbirdrcm --
rcm_install() { # STAGE RCM_DIR
  local stage="$1" dir="$2"
  [[ -f "$stage/bin/hqbirdrcm" ]] || die "missing $stage/bin/hqbirdrcm"
  [[ -f "$stage/conf/rcm.json" ]] || die "missing $stage/conf/rcm.json"
  log "hqbirdrcm -> $dir"
  id hqbirdrcm >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin hqbirdrcm
  mkdir -p "$dir/certs" "$dir/rcm-data"
  if [[ -f "$dir/rcm.json" ]]; then "$dir/hqbirdrcm" svc stop -config "$dir/rcm.json" 2>/dev/null || true; fi
  install -m 0755 "$stage/bin/hqbirdrcm" "$dir/hqbirdrcm"
  install -m 0640 "$stage/conf/rcm.json" "$dir/rcm.json"
  cp -f "$stage/rcm-certs/"* "$dir/certs/"
  chown -R hqbirdrcm:hqbirdrcm "$dir"
  chmod 750 "$dir/certs"; chmod 640 "$dir/certs/"*
  "$dir/hqbirdrcm" svc uninstall -config "$dir/rcm.json" >/dev/null 2>&1 || true
  "$dir/hqbirdrcm" svc install -config "$dir/rcm.json" -user hqbirdrcm -group hqbirdrcm
  "$dir/hqbirdrcm" svc start -config "$dir/rcm.json"
  sleep 2
  systemctl is-active hqbirdrcm >/dev/null || die "hqbirdrcm service is not active"
  log "rcm active"
}

rcm_uninstall() { # RCM_DIR
  local dir="$1"
  if [[ -f "$dir/rcm.json" && -x "$dir/hqbirdrcm" ]]; then
    "$dir/hqbirdrcm" svc stop -config "$dir/rcm.json" 2>/dev/null || true
    "$dir/hqbirdrcm" svc uninstall -config "$dir/rcm.json" || warn "rcm svc uninstall failed"
  fi
  if unit_exists hqbirdrcm; then
    systemctl disable --now hqbirdrcm 2>/dev/null || true
    rm -f /etc/systemd/system/hqbirdrcm.service
  fi
  systemctl daemon-reload
  rm -rf "$dir"
  log "rcm removed from $dir"
}
