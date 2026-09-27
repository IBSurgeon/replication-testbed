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
FBA_PORT="$(arg fbagent_port 13050)"
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

  uninstall)
    if has "$COMPONENTS" rcm; then rcm_uninstall "$RCM_DIR"; fi
    if has "$COMPONENTS" node; then node_uninstall "$NODE_DIR"; fi
    if has "$COMPONENTS" fbagent; then
      fbagent_uninstall "$FBA_DIR" "$FBA_SERVICE"
      fb_restore_pristine_conf "$FB_ROOT" "$FB_UNIT"
    fi
    result '{"uninstalled":true}'
    ;;

  agent-id)
    result "{\"agent_id\":\"$(json_get "$FBA_DIR/agent_config.json" goafts.agent_id)\",\"hostname\":\"$(hostname -s)\"}"
    ;;

  *) die "usage: 20-goafts.sh download|enroll|install|uninstall|agent-id [--key value ...]" ;;
esac
