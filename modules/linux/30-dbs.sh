#!/usr/bin/env bash
# Module 30 (Linux): test databases on the master, and their removal.
#
#   30-dbs.sh prepare --db-root DIR [--subdir tb] [--count 2] [--file-name employee.fdb]
#                     [--source FILE] [--fb-root /opt/firebird] [--force false]
#   30-dbs.sh remove  --db-root DIR [--subdir tb]
#   30-dbs.sh list    --db-root DIR [--subdir tb]
#
# prepare makes <db-root>/<subdir>/db1..dbN/<file-name> from --source
# (default: the EMPLOYEE example of Firebird). Copies are taken under an
# nbackup lock (-L ... -N), so each is consistent, and -F turns each copy
# into a standalone database with its own GUID.
#
# Making them replicate (scansync, Firebird restart, publication) is done by
# tb.py through the node API, because it needs the running node.
#
# remove deletes the whole <db-root>/<subdir> tree, journal folders included.
# On a replica pass --db-root <replica root>/<master node id>.
source "$(dirname "$0")/common.sh"

CMD="${1:-}"; shift || true
parse_args "$@"
need_root

DB_ROOT="$(arg db_root)"; [[ -n "$DB_ROOT" ]] || die "--db-root is required"
SUBDIR="$(arg subdir tb)"
DIR="$DB_ROOT/$SUBDIR"
[[ "$SUBDIR" != "" && "$SUBDIR" != "." && "$SUBDIR" != ".." && "$SUBDIR" != */* ]] \
  || die "--subdir must be one folder name"

list_json() {
  python3 - "$DIR" <<'PY'
import glob, json, os, sys
d = sys.argv[1]
files = sorted(glob.glob(os.path.join(d, "*", "*.fdb")) + glob.glob(os.path.join(d, "*", "*.FDB")))
print("TBRESULT " + json.dumps({"dir": d, "files": files}))
PY
}

case "$CMD" in
  prepare)
    load_secrets
    FB_ROOT="$(arg fb_root /opt/firebird)"
    COUNT="$(arg count 2)"
    NAME="$(arg file_name employee.fdb)"
    SRC="$(arg source "$FB_ROOT/examples/empbuild/employee.fdb")"
    NB="$(fb_tool "$FB_ROOT" nbackup)"
    [[ -f "$SRC" ]] || die "no source database $SRC"
    [[ -x "$NB" ]] || die "no nbackup in $FB_ROOT"
    [[ "$COUNT" =~ ^[0-9]+$ && "$COUNT" -ge 1 ]] || die "--count must be >= 1"
    mkdir -p "$DIR"
    todo=()
    for i in $(seq 1 "$COUNT"); do
      f="$DIR/db$i/$NAME"
      if [[ -f "$f" && "$(arg force false)" != "true" ]]; then log "exists, kept: $f"; continue; fi
      todo+=("$f")
    done
    if [[ ${#todo[@]} -gt 0 ]]; then
      log "copy $SRC -> ${#todo[@]} database(s) under nbackup lock"
      "$NB" -L "$SRC"
      ec=0
      for f in "${todo[@]}"; do
        mkdir -p "$(dirname "$f")"
        cp -f "$SRC" "$f" || ec=1
      done
      "$NB" -N "$SRC"          # always unlock, even when a copy failed
      (( ec == 0 )) || die "copy under nbackup lock failed"
      [[ ! -f "$SRC.delta" ]] || die "leftover $SRC.delta after unlock"
      for f in "${todo[@]}"; do "$NB" -F "$f"; done
    fi
    chown -R firebird:firebird "$DB_ROOT"
    list_json
    ;;

  remove)
    if [[ -d "$DIR" ]]; then
      rm -rf "$DIR"
      log "removed $DIR"
    else
      log "nothing to remove: $DIR"
    fi
    result "{\"removed\":\"$DIR\"}"
    ;;

  list) list_json ;;

  *) die "usage: 30-dbs.sh prepare|remove|list [--key value ...]" ;;
esac
