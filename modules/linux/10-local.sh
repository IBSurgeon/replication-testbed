#!/usr/bin/env bash
# Module 10 (Linux): install from LOCAL COPIES of the binaries, no goafts
# enrollment; and remove what it installed.
#
#   10-local.sh detect
#   10-local.sh install   --components fbagent,node,rcm --stage DIR
#                         [--fb-root /opt/firebird] [--fb-port 3050] [--fb-service UNIT]
#                         [--fbagent-mode install|existing] [--fbagent-dir DIR]
#                         [--fbagent-port 13050] [--fbagent-instance ID] [--fbagent-service hqbirdfbagent]
#                         [--node-dir /opt/hqclusternode] [--db-root DIR] [--node-user root]
#                         [--rcm-dir /opt/hqbirdrcm]
#   10-local.sh uninstall --components fbagent,node,rcm [same dirs] [--restore-conf true|false]
#   10-local.sh wipe      [same dirs]   (everything the test bed installed; checks what is left)
#   10-local.sh fbagent-info [--fbagent-dir DIR]
#
# The stage folder holds bin/, conf/, certs/, rcm-certs/ (see products.sh).
# fbagent-mode existing: keep the fbagent already on the host; only check it.
source "$(dirname "$0")/common.sh"
source "$(dirname "$0")/products.sh"

CMD="${1:-}"; shift || true
parse_args "$@"
need_root

FB_ROOT="$(arg fb_root /opt/firebird)"
FB_PORT="$(arg fb_port 3050)"
FB_UNIT="$(arg fb_service)"
FBA_MODE="$(arg fbagent_mode install)"
FBA_DIR="$(arg fbagent_dir /opt/hqbird-fbagent)"
FBA_PORT="$(arg fbagent_port 13055)"
FBA_INSTANCE="$(arg fbagent_instance "tb-$(hostname -s)-$FB_PORT")"
FBA_SERVICE="$(arg fbagent_service hqbirdfbagent)"
NODE_DIR="$(arg node_dir /opt/hqclusternode)"
RCM_DIR="$(arg rcm_dir /opt/hqbirdrcm)"
DB_ROOT="$(arg db_root)"
COMPONENTS="$(arg components fbagent,node)"
STAGE="$(arg stage "$TB_WORK/stage")"

[[ -n "$FB_UNIT" ]] || FB_UNIT="$(detect_fb_unit)"

case "$CMD" in
  detect)
    # Facts tb.py needs before it renders node.json. Nothing is changed.
    ver=""
    if [[ -x "$(fb_tool "$FB_ROOT" isql)" ]]; then
      ver="$("$(fb_tool "$FB_ROOT" isql)" -z </dev/null 2>/dev/null | head -n1 | tr -d '\r' || true)"
    fi
    ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | sed 's/.*/"&"/' | paste -sd, - || true)"
    result "{\"hostname\":\"$(hostname -s)\",\"hostname_full\":\"$(uname -n)\",\"ips\":[${ips}],\"fb_conf_port\":\"$(fb_conf_port "$FB_ROOT")\",\"fb_unit\":\"$FB_UNIT\",\"fb_root_ok\":$([[ -x "$(fb_tool "$FB_ROOT" isql)" ]] && echo true || echo false),\"isql\":\"${ver//\"/}\",\"python3\":$(command -v python3 >/dev/null && echo true || echo false)}"
    ;;

  install)
    load_secrets
    need_cmd python3; need_cmd curl
    [[ -n "$FB_UNIT" ]] || die "cannot find the Firebird systemd unit; pass --fb-service"
    [[ -x "$(fb_tool "$FB_ROOT" isql)" ]] || die "no Firebird in $FB_ROOT (install HQbird/Firebird first)"
    fb_save_pristine_conf "$FB_ROOT"
    if has "$COMPONENTS" fbagent; then
      if [[ "$FBA_MODE" == "install" ]]; then
        fbagent_install_local "$STAGE" "$FBA_DIR" "$FB_ROOT" "$FB_PORT" "$FB_UNIT" \
          "$FBA_PORT" "$FBA_INSTANCE" "$FBA_SERVICE"
      else
        log "fbagent mode 'existing': checking the agent on 127.0.0.1:$FBA_PORT"
        fbagent_check "$FBA_PORT" "$FBA_INSTANCE" "$FB_PORT" || die "existing fbagent check failed"
      fi
    fi
    if has "$COMPONENTS" node; then
      [[ -n "$DB_ROOT" ]] || die "--db-root is required for the node"
      node_install "$STAGE" "$NODE_DIR" "$DB_ROOT" "$(arg node_user root)"
    fi
    if has "$COMPONENTS" rcm; then rcm_install "$STAGE" "$RCM_DIR"; fi
    # node.json / rcm.json hold the Firebird password: keep them only in place.
    rm -rf "$STAGE/conf" "$STAGE/certs" "$STAGE/rcm-certs"
    result '{"installed":true}'
    ;;

  uninstall|wipe)
    # uninstall: the components asked for. wipe: everything the test bed puts
    # on a host -- load processes, tb-block firewall rules, rcm, node, and an
    # fbagent it installed (an 'existing' agent is kept). Both end by checking
    # what is left and fail when anything is.
    if [[ "$CMD" == wipe ]]; then
      COMPONENTS="fbagent,node,rcm"
      kill_under "$TB_WORK/load"; kill_under "$TB_WORK/loadgen"
      if command -v iptables >/dev/null; then
        while rule="$(iptables -S 2>/dev/null | grep -m1 'tb-block')" && [[ -n "$rule" ]]; do
          log "remove firewall rule: $rule"
          eval "iptables ${rule/-A /-D }" || break
        done
      fi
    fi
    if has "$COMPONENTS" rcm; then rcm_uninstall "$RCM_DIR"; fi
    if has "$COMPONENTS" node; then node_uninstall "$NODE_DIR"; fi
    if has "$COMPONENTS" fbagent && [[ "$FBA_MODE" == "install" ]]; then
      fbagent_uninstall "$FBA_DIR" "$FBA_SERVICE" "$FB_PORT"
    fi
    if [[ "$(arg restore_conf true)" == "true" ]]; then fb_restore_pristine_conf "$FB_ROOT" "$FB_UNIT"; fi
    {
      if has "$COMPONENTS" rcm; then leftovers_of rcm "$RCM_DIR"; unit_exists hqbirdrcm && echo "rcm: unit hqbirdrcm.service"; fi
      if has "$COMPONENTS" node; then leftovers_of node "$NODE_DIR"; fi
      if has "$COMPONENTS" fbagent && [[ "$FBA_MODE" == "install" ]]; then
        fbagent_leftovers "$FBA_DIR" "$FBA_SERVICE" "$FB_PORT"
      fi
      if [[ "$CMD" == wipe ]]; then
        for p in $(pids_under "$TB_WORK"); do echo "work: process $p $(tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | cut -c1-120)"; done
        iptables -S 2>/dev/null | grep tb-block | sed 's/^/firewall: /' || true
      fi
      true
    } | report_uninstall
    ;;

  fbagent-info)
    # Prints local_api settings of an existing agent. The token is a secret:
    # it goes on a TBSECRET line, which tb.py keeps out of its log.
    cfg="$FBA_DIR/agent_config.json"
    [[ -f "$cfg" ]] || die "no $cfg"
    result "{\"listen\":\"$(json_get "$cfg" local_api.listen)\",\"instance_id\":\"$(json_get "$cfg" local_api.instance_id)\",\"enabled\":\"$(json_get "$cfg" local_api.enabled)\"}"
    echo "TBSECRET fbagent_token=$(json_get "$cfg" local_api.token)"
    ;;

  *) die "usage: 10-local.sh detect|install|uninstall|wipe|fbagent-info [--key value ...]" ;;
esac
