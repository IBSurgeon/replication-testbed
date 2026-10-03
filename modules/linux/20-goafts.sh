#!/usr/bin/env bash
# Module 20 (Linux): install everything FROM A GOAFTS SERVER, and remove it.
#
#   20-goafts.sh download  --url https://HOST:9443 --pin HEX [--channel stable]
#                          [--products fbagent,hqclusternode,hqbirdrcm] [--stage DIR]
#   20-goafts.sh enroll    --url URL --pin HEX [--enroll-timeout 30m]
#                          [--fb-root /opt/firebird] [--fb-port 3050] [--fb-service UNIT]
#                          [--fbagent-dir /opt/hqbird-fbagent] [--fbagent-port 13050]
#                          [--fbagent-instance ID] [--fbagent-service hqbirdfbagent]
#   20-goafts.sh install   --components node,rcm [--product-install direct|agent]
#                          [--channel stable] [--stage DIR] [--node-dir DIR] [--db-root DIR]
#                          [--rcm-dir DIR] [--fbagent-dir DIR] [--fbagent-service NAME]
#   20-goafts.sh installer --script FILE [--goafts chess1] [--channel stable] [fbagent args]
#   20-goafts.sh cluster-prep [--bases /opt/hqclusternode,/opt/hqbirdrcm] [--db-root DIR]
#   20-goafts.sh channel   [--product-channel CH] [--self-update on|off] [fbagent args]
#   20-goafts.sh agent-swap --binary FILE | --restore true [fbagent args]
#   20-goafts.sh uninstall --components fbagent,node,rcm [dirs as above]
#   20-goafts.sh agent-id  [--fbagent-dir DIR]
#
# --pin is the SPKI SHA-256 pin of the goafts TLS certificate (64 hex chars),
# the value of GET <url>/v1/bootstrap/pin. Every download is checked against
# it (curl --pinnedpubkey) and against the sha256 in the release metadata.
#
# enroll blocks until an administrator approves the agent's CSR on goafts
# (tb.py approves it itself when goafts.admin is configured).
source "$(dirname "$0")/common.sh"
source "$(dirname "$0")/products.sh"

CMD="${1:-}"; shift || true
parse_args "$@"
need_root

URL="$(arg url)"; URL="${URL%/}"
PIN="$(arg pin)"
CHANNEL="$(arg channel stable)"
STAGE="$(arg stage "$TB_WORK/stage")"
FB_ROOT="$(arg fb_root /opt/firebird)"
FB_PORT="$(arg fb_port 3050)"
FB_UNIT="$(arg fb_service)"; [[ -n "$FB_UNIT" ]] || FB_UNIT="$(detect_fb_unit)"
FBA_DIR="$(arg fbagent_dir /opt/hqbird-fbagent)"
FBA_PORT="$(arg fbagent_port 13055)"
FBA_INSTANCE="$(arg fbagent_instance "tb-$(hostname -s)-$FB_PORT")"
FBA_SERVICE="$(arg fbagent_service hqbirdfbagent)"
NODE_DIR="$(arg node_dir /opt/hqclusternode)"
RCM_DIR="$(arg rcm_dir /opt/hqbirdrcm)"
COMPONENTS="$(arg components node,rcm)"

need_pin() {
  [[ "$URL" == https://* ]] || die "--url must be https://HOST:9443"
  [[ "$PIN" =~ ^[0-9a-fA-F]{64}$ ]] || die "--pin must be 64 hex characters (SPKI SHA-256)"
}

pin_b64() { python3 -c 'import base64,sys; print(base64.b64encode(bytes.fromhex(sys.argv[1])).decode())' "$PIN"; }

# goafts_get PATH OUTFILE : pinned HTTPS GET (-k: the chain is not trusted,
# the SPKI pin is what authenticates the server).
goafts_get() {
  curl -fsS -k --pinnedpubkey "sha256//$(pin_b64)" --retry 3 -o "$2" "$URL$1"
}

download_product() { # PRODUCT DEST
  local p="$1" dest="$2" meta sha ver got
  meta="$(mktemp)"
  goafts_get "/v1/bootstrap/releases/$p/linux-amd64?channel=$CHANNEL" "$meta" \
    || die "no $p release for linux-amd64 on channel $CHANNEL (or the pin does not match)"
  ver="$(json_get "$meta" version)"; sha="$(json_get "$meta" sha256)"
  rm -f "$meta"
  [[ -n "$sha" ]] || die "release metadata of $p has no sha256"
  goafts_get "/v1/bootstrap/download/$p/linux-amd64?channel=$CHANNEL" "$dest.part"
  got="$(sha256sum "$dest.part" | awk '{print $1}')"
  [[ "$got" == "$sha" ]] || { rm -f "$dest.part"; die "$p sha256 mismatch: want $sha got $got"; }
  mv -f "$dest.part" "$dest"; chmod 0755 "$dest"
  log "$p $ver downloaded (sha256 OK)"
  LAST_VERSION="$ver"
}

case "$CMD" in
  download)
    need_pin; need_cmd curl; need_cmd python3
    mkdir -p "$STAGE/bin"
    vers="{"
    IFS=',' read -r -a PRODUCTS <<<"$(arg products fbagent,hqclusternode,hqbirdrcm)"
    for p in "${PRODUCTS[@]}"; do
      download_product "$p" "$STAGE/bin/$p"
      vers+="\"$p\":\"$LAST_VERSION\","
    done
    result "${vers%,}}"
    ;;

  enroll)
    need_pin; load_secrets; need_cmd python3; need_cmd curl
    [[ -f "$STAGE/bin/fbagent" ]] || die "run 'download' first: no $STAGE/bin/fbagent"
    [[ -n "$FB_UNIT" ]] || die "cannot find the Firebird systemd unit; pass --fb-service"
    fb_save_pristine_conf "$FB_ROOT"
    id firebird >/dev/null 2>&1 || die "no 'firebird' user: install Firebird first"
    systemctl stop "$FBA_SERVICE" 2>/dev/null || true
    mkdir -p "$FBA_DIR"/{certs,logs}
    install -m 0755 "$STAGE/bin/fbagent" "$FBA_DIR/fbagent"
    chown -R firebird:firebird "$FBA_DIR"
    if [[ ! -f "$FBA_DIR/agent_config.json" ]]; then SETUP=(--setup "$FB_ROOT"); else SETUP=(--enroll); fi
    log "fbagent ${SETUP[*]} --bootstrap-url $URL (waits for CSR approval, up to $(arg enroll_timeout 30m))"
    su -s /bin/sh firebird -c "cd '$FBA_DIR' && ./fbagent ${SETUP[*]} --bootstrap-url '$URL' --server-pin '$PIN' --enroll-timeout '$(arg enroll_timeout 30m)'" \
      || die "fbagent enrollment failed"
    fbagent_patch_local_api "$FBA_DIR" "$FBA_PORT" "$FBA_INSTANCE"
    chown -R firebird:firebird "$FBA_DIR"
    (cd "$FBA_DIR" && ./fbagent --install agent_config.json)
    systemctl enable --now "$FBA_SERVICE"
    wait_until 60 fbagent_check "$FBA_PORT" "$FBA_INSTANCE" "$FB_PORT" || die "fbagent local_api does not answer"
    result "{\"agent_id\":\"$(json_get "$FBA_DIR/agent_config.json" goafts.agent_id)\",\"server_url\":\"$(json_get "$FBA_DIR/agent_config.json" goafts.server_url)\"}"
    ;;

  install)
    load_secrets; need_cmd python3
    MODE="$(arg product_install direct)"
    DB_ROOT="$(arg db_root)"
    if [[ "$MODE" == "direct" ]]; then
      # Binaries came from goafts (download); register the services ourselves.
      if has "$COMPONENTS" node; then
        [[ -n "$DB_ROOT" ]] || die "--db-root is required for the node"
        node_install "$STAGE" "$NODE_DIR" "$DB_ROOT" "$(arg node_user root)"
      fi
      if has "$COMPONENTS" rcm; then rcm_install "$STAGE" "$RCM_DIR"; fi
    else
      # fbagent installs the products itself: provision config + certs, allow
      # install in agent_config.json, then ask for an update now.
      for c in node rcm; do
        has "$COMPONENTS" "$c" || continue
        if [[ "$c" == node ]]; then id=hqclusternode; dir="$NODE_DIR"; conf=node.json; certs="$STAGE/certs"
          mkdir -p "$DB_ROOT"; chown -R firebird:firebird "$DB_ROOT"
        else id=hqbirdrcm; dir="$RCM_DIR"; conf=rcm.json; certs="$STAGE/rcm-certs"; fi
        mkdir -p "$dir/certs"
        install -m 0640 "$STAGE/conf/$conf" "$dir/$conf"
        cp -f "$certs/"* "$dir/certs/"
        ID="$id" DIR="$dir" CONF="$conf" CH="$CHANNEL" CFG="$FBA_DIR/agent_config.json" python3 - <<'PY'
import json, os
e = os.environ
cfg = json.load(open(e["CFG"], encoding="utf-8"))
p = cfg.setdefault(e["ID"], {})
p["install_path"] = e["DIR"]
p["config_path"] = os.path.join(e["DIR"], e["CONF"])
p["certs_path"] = os.path.join(e["DIR"], "certs")
u = p.setdefault("update", {})
u.update({"enabled": True, "install_enabled": True, "apply_automatically": True, "channel": e["CH"]})
json.dump(cfg, open(e["CFG"], "w", encoding="utf-8"), indent=2)
print("agent_config.json:", e["ID"], "install enabled")
PY
      done
      chown -R firebird:firebird "$FBA_DIR"
      systemctl restart "$FBA_SERVICE"; sleep 3
      for c in node rcm; do
        has "$COMPONENTS" "$c" || continue
        id=hqclusternode; [[ "$c" == rcm ]] && id=hqbirdrcm
        log "fbagent --product-update $id --apply"
        (cd "$FBA_DIR" && ./fbagent --product-update "$id" --apply) || die "fbagent could not install $id"
        (cd "$FBA_DIR" && ./fbagent --product-status "$id") || true
      done
      if has "$COMPONENTS" node; then
        wait_until 120 "$NODE_DIR/hqclusternode" healthcheck -config "$NODE_DIR/node.json" -certs "$NODE_DIR/certs" \
          || die "node installed by fbagent does not answer /v1/health"
      fi
    fi
    # node.json / rcm.json hold the Firebird password: keep them only in place.
    rm -rf "$STAGE/conf" "$STAGE/certs" "$STAGE/rcm-certs"
    result '{"installed":true}'
    ;;

  installer)
    # The customer's way onto a bare host: fbagent's own installer
    # (ops/linux-install/fbagent-fbXX_known.sh, --script) installs HQbird
    # from its tarball (no replconf plugin, no replconf.properties), fbagent
    # from goafts --goafts, enrolls it (waits for the CSR) and makes the
    # cluster folders (--cluster). Then the test bed's local_api settings.
    load_secrets
    SCRIPT="$(arg script)"
    [[ -f "$SCRIPT" ]] || die "--script $SCRIPT not found"
    log "fbagent installer $(basename "$SCRIPT") --goafts $(arg goafts chess1) --channel $CHANNEL (waits for CSR approval)"
    SYSDBA_PASS="$TB_FB_PASSWORD" bash "$SCRIPT" --goafts "$(arg goafts chess1)" --channel "$CHANNEL" \
      --enroll --cluster --skip-apt-upgrade --enroll-timeout "$(arg enroll_timeout 30m)" </dev/null \
      || die "the fbagent installer failed"
    [[ -f "$FBA_DIR/agent_config.json" ]] || die "the installer left no $FBA_DIR/agent_config.json"
    fbagent_patch_local_api "$FBA_DIR" "$FBA_PORT" "$FBA_INSTANCE"
    chown firebird:firebird "$FBA_DIR/agent_config.json"
    systemctl restart "$FBA_SERVICE"
    wait_until 60 fbagent_check "$FBA_PORT" "$FBA_INSTANCE" "$FB_PORT" || die "fbagent local_api does not answer"
    result "{\"agent_id\":\"$(json_get "$FBA_DIR/agent_config.json" goafts.agent_id)\"}"
    ;;

  cluster-prep)
    # A goafts cluster: the agent (user firebird) writes each member's key,
    # certificates and node.json / rcm.json into <base>/<role> and
    # /opt/hqbirdrcm and installs the products there itself, but it cannot
    # create folders in /opt. Make the base folders, as the fbagent
    # installer's --cluster does, and the databases root.
    id firebird >/dev/null 2>&1 || die "no 'firebird' user: install Firebird first"
    IFS=',' read -r -a BASES <<<"$(arg bases /opt/hqclusternode,/opt/hqbirdrcm)"
    for d in "${BASES[@]}"; do
      [[ -d "$d" ]] || install -d -o firebird -g firebird -m 0750 "$d"
      chown firebird:firebird "$d"
    done
    DB_ROOT="$(arg db_root)"
    [[ -z "$DB_ROOT" ]] || install -d -o firebird -g firebird -m 2770 "$DB_ROOT"
    # fbagent --setup writes SYSDBA with Firebird's stock password; the agent copies its
    # credentials into the node.json it writes for a cluster member (only
    # where node.json has none). Put this host's SYSDBA password in both, as
    # the fbagent installer does with --sysdba-pass.
    load_secrets
    changed="$(CFG="$FBA_DIR/agent_config.json" NODE_JSON="$(arg node_dir)/node.json" python3 - <<'PY'
import base64, json, os
pw = os.environ.get("TB_FB_PASSWORD", "")
out = []
def save(path, doc):
    st = os.stat(path)
    tmp = path + ".tb-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.chmod(tmp, st.st_mode & 0o7777)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
cfg_path, node_path = os.environ["CFG"], os.environ["NODE_JSON"]
if pw and os.path.isfile(cfg_path):
    cfg = json.load(open(cfg_path, encoding="utf-8"))
    cred = cfg.setdefault("firebird", {}).setdefault("credentials", {})
    enc = base64.b64encode(pw.encode()).decode()
    if str(cred.get("username") or "SYSDBA").upper() == "SYSDBA" and cred.get("password_encrypted") != enc:
        cred.setdefault("username", "SYSDBA")
        cred["password_encrypted"] = enc
        save(cfg_path, cfg)
        out.append("agent")
if pw and os.path.isfile(node_path):
    doc = json.load(open(node_path, encoding="utf-8"))
    fb = doc.get("firebird") or {}
    if str(fb.get("user") or "SYSDBA").upper() == "SYSDBA" and fb.get("password") != pw:
        fb["password"] = pw
        doc["firebird"] = fb
        save(node_path, doc)
        out.append("node")
print(",".join(out))
PY
)"
    if has "$changed" agent; then
      systemctl restart "$FBA_SERVICE"
      wait_until 60 fbagent_check "$FBA_PORT" "$FBA_INSTANCE" "$FB_PORT" || die "fbagent local_api does not answer"
    fi
    if has "$changed" node; then
      "$(arg node_dir)/hqclusternode" svc stop -config "$(arg node_dir)/node.json" 2>/dev/null || true
      "$(arg node_dir)/hqclusternode" svc start -config "$(arg node_dir)/node.json" 2>/dev/null || true
    fi
    log "SYSDBA password set in: ${changed:-nothing (already so)}"
    result "{\"bases\":\"$(arg bases /opt/hqclusternode,/opt/hqbirdrcm)\",\"db_root\":\"$DB_ROOT\",\"password_set\":\"$changed\"}"
    ;;

  channel)
    # --product-channel CH: the update channel of hqclusternode and hqbirdrcm
    # in agent_config.json. --self-update on|off: whether the agent updates
    # itself (goafts.auto_update.enabled). Then the agent is restarted.
    load_secrets
    CFG="$FBA_DIR/agent_config.json"
    [[ -f "$CFG" ]] || die "no $CFG: enroll first"
    PCH="$(arg product_channel)" SELF="$(arg self_update)" python3 - "$CFG" <<'PY'
import json, os, sys
p = sys.argv[1]
cfg = json.load(open(p, encoding="utf-8"))
ch, self_up = os.environ["PCH"], os.environ["SELF"]
if ch:
    for prod in ("hqclusternode", "hqbirdrcm"):
        cfg.setdefault(prod, {}).setdefault("update", {})["channel"] = ch
if self_up in ("on", "off"):
    cfg.setdefault("goafts", {}).setdefault("auto_update", {})["enabled"] = self_up == "on"
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2)
print("agent_config.json: product channel", ch or "(kept)", "; self-update", self_up or "(kept)")
PY
    chown firebird:firebird "$CFG"
    systemctl restart "$FBA_SERVICE"
    wait_until 60 fbagent_check "$FBA_PORT" "$FBA_INSTANCE" "$FB_PORT" || die "fbagent local_api does not answer"
    result "{\"product_channel\":\"$(arg product_channel)\",\"self_update\":\"$(arg self_update)\"}"
    ;;

  agent-swap)
    # --binary FILE: run this fbagent build in place of the installed one
    # (an older agent, T4); the installed binary is kept as fbagent.tb-saved.
    # --restore true: put the saved binary back. Turn self-update off first
    # (channel --self-update off), or the agent replaces itself again.
    load_secrets
    SAVED="$FBA_DIR/fbagent.tb-saved"
    systemctl stop "$FBA_SERVICE"
    if [[ "$(arg restore false)" == true ]]; then
      [[ -f "$SAVED" ]] || die "nothing to restore: no $SAVED"
      mv -f "$SAVED" "$FBA_DIR/fbagent"
    else
      BIN="$(arg binary)"
      [[ -f "$BIN" ]] || die "--binary $BIN not found"
      [[ -f "$SAVED" ]] || cp -p "$FBA_DIR/fbagent" "$SAVED"
      install -m 0755 "$BIN" "$FBA_DIR/fbagent"
    fi
    chown firebird:firebird "$FBA_DIR/fbagent"
    since="$(date '+%Y-%m-%d %H:%M:%S')"
    systemctl start "$FBA_SERVICE"
    wait_until 60 fbagent_check "$FBA_PORT" "$FBA_INSTANCE" "$FB_PORT" || die "fbagent local_api does not answer"
    # The version from the agent's start line ("fbagent --version" would start
    # a second agent).
    ver="$(journalctl -u "$FBA_SERVICE" --since "$since" --no-pager 2>/dev/null \
           | grep -oE 'version[ =:"]+[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*' | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*')"
    result "{\"version\":\"$ver\",\"restored\":$(arg restore false)}"
    ;;

  uninstall)
    if has "$COMPONENTS" rcm; then rcm_uninstall "$RCM_DIR"; fi
    if has "$COMPONENTS" node; then node_uninstall "$NODE_DIR"; fi
    if has "$COMPONENTS" fbagent; then
      fbagent_uninstall "$FBA_DIR" "$FBA_SERVICE" "$FB_PORT"
      fb_restore_pristine_conf "$FB_ROOT" "$FB_UNIT"
    fi
    {
      if has "$COMPONENTS" rcm; then leftovers_of rcm "$RCM_DIR"; unit_exists hqbirdrcm && echo "rcm: unit hqbirdrcm.service"; fi
      if has "$COMPONENTS" node; then leftovers_of node "$NODE_DIR"; fi
      if has "$COMPONENTS" fbagent; then fbagent_leftovers "$FBA_DIR" "$FBA_SERVICE" "$FB_PORT"; fi
      true
    } | report_uninstall
    ;;

  agent-id)
    result "{\"agent_id\":\"$(json_get "$FBA_DIR/agent_config.json" goafts.agent_id)\",\"hostname\":\"$(hostname -s)\"}"
    ;;

  *) die "usage: 20-goafts.sh download|enroll|install|installer|cluster-prep|channel|agent-swap|uninstall|agent-id [--key value ...]" ;;
esac
