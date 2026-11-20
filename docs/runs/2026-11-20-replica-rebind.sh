#!/usr/bin/env bash
# Stage 0 of hqcluster-node docs/switchover-keep-replicas-plan.ru.md (variant B), plain Firebird 4/5,
# no node. Three databases on one server: A master, B and C replicas; a shell
# "shipper" copies archived segments. Usage: 2026-11-20-replica-rebind.sh s1|r1|r2|r3
#   s1  switchover A->B, C and A re-bound in place (nbackup -F, source_guid=G2)
#   r1  C NOT re-bound (no nbackup -F), only source_guid=G2: expect silent loss
#   r2  C re-bound, but G2 segment 1 never reaches C: expect silent loss
#   r3  C behind (last A segment not shipped to C), re-bound anyway: expect loss
set -u
SC=${1:?scenario}
FB=/opt/firebird
: "${FB_PASSWORD:?set FB_PASSWORD to the SYSDBA password}"
U="-user SYSDBA -password $FB_PASSWORD"
D=/data
SVC=$(systemctl list-unit-files | grep -oE '^firebird[^ ]*\.service' | head -1)
LOG=$FB/replication.log

say() { echo "[$(date +%T)] $*"; }
sql() { local db=$1; shift; printf 'set heading off;\n%s\n' "$*" | $FB/bin/isql -q $U "localhost:$D/$db.fdb" 2>&1 | sed '/^$/d'; }
digest() { sql $1 "select count(*)||':'||coalesce(sum(id),0)||':'||coalesce(sum(g),0)||':'||coalesce(sum(mod(hash(v),1000003)),0) from t;" | tr -d ' '; }
hdr() { $FB/bin/gstat -h localhost:$D/$1.fdb $U 2>&1 | grep -iE "guid|sequence|replica|shutdown|backup state" | sed "s/^/  $1: /"; }
guid() { $FB/bin/gstat -h localhost:$D/$1.fdb $U 2>&1 | grep -i "database guid" | grep -oE '\{[^}]+\}'; }
gfix() { $FB/bin/gfix "$@" $U; }
restart() { systemctl restart $SVC; sleep 3; }

# writeconf a=<role> b=<role> c=<role>; role = master | replica[:{guid}] | none
writeconf() {
  : > $FB/replication.conf
  for kv in "$@"; do
    local x=${kv%%=*} r=${kv#*=}
    case $r in
      master) printf 'database = %s/%s.fdb\n{\n  journal_directory = %s/%s.jrn\n  journal_archive_directory = %s/%s.arch\n  journal_archive_timeout = 2\n}\n' $D $x $D $x $D $x ;;
      replica*) printf 'database = %s/%s.fdb\n{\n  journal_source_directory = %s/%s.src\n  apply_idle_timeout = 1\n  apply_error_timeout = 3\n  verbose_logging = true\n' $D $x $D $x
                [ "${r#replica:}" != "$r" ] && printf '  source_guid = "%s"\n' "${r#replica:}"
                printf '}\n' ;;
    esac
  done >> $FB/replication.conf
  chown firebird:firebird $FB/replication.conf
}

# ship X "Y Z" [skip_first_for=Y]: copy archived segments of X older than 1 s
ship() {
  local x=$1 skipfor=${3:-}
  for f in $(ls $D/$x.arch 2>/dev/null | sort); do
    [ $(( $(date +%s) - $(stat -c %Y $D/$x.arch/$f) )) -ge 1 ] || continue
    for y in $2; do
      grep -qx "$f" $D/ship.$x.$y 2>/dev/null && continue
      echo "$f" >> $D/ship.$x.$y
      if [ "$y" = "$skipfor" ] && [ ! -e $D/skipped.$x.$y ]; then touch $D/skipped.$x.$y; say "SKIP $f -> $y"; continue; fi
      cp $D/$x.arch/$f $D/$y.src/.tmp && chown firebird:firebird $D/$y.src/.tmp && mv $D/$y.src/.tmp $D/$y.src/$f
    done
  done
}

# load X phase batches: inserts, updates, deletes; one commit per batch, 1 s apart
load() {
  local x=$1 p=$2 n=$3
  for b in $(seq 1 $n); do
    sql $x "set term ^;
    execute block as declare i int = 0; begin
      while (i < 300) do begin
        insert into t(id, g, v) values ($p * 1000000 + $b * 1000 + :i, $p, lpad('', mod(:i, 37) + 1, 'x') || '$p.$b');
        i = i + 1; end
      update t set v = v || 'u' where g = $p and mod(id, 7) = mod($b, 7);
      delete from t where mod(id, 11) = 0 and g = $p - 1;
    end^
    set term ;^
    commit;" | grep -iE "error|failed" | head -3
    sleep 1
  done
}

# waitsame M "Y Z" secs [ship X "Y Z" skip]: wait until digest(Y) = expected
waitsame() {
  local want=$1 ys=$2 t=$3; shift 3
  for i in $(seq 1 $t); do
    [ $# -gt 0 ] && ship "$@"
    local ok=1
    for y in $ys; do [ "$(digest $y)" = "$want" ] || ok=0; done
    [ $ok = 1 ] && { say "SAME after ${i}s: $ys = $want"; return 0; }
    sleep 1
  done
  for y in $ys; do say "DIFF $y = $(digest $y), want $want"; done
  return 1
}

# rebind X: the variant B steps on replica X (database in single shutdown first)
rebind() {
  local x=$1
  gfix -shut single -force 0 localhost:$D/$x.fdb
  if [ "$x" = c ]; then
    say "V-2: nbackup -L on c while still replica read_only"
    if $FB/bin/nbackup $U -L localhost:$D/c.fdb 2>&1 | sed 's/^/  /'; then
      say "V-2: nbackup -L on read_only replica WORKED"; $FB/bin/nbackup $U -N localhost:$D/c.fdb
    else say "V-2: nbackup -L on read_only replica FAILED"; fi
  fi
  gfix -replica none localhost:$D/$x.fdb
  $FB/bin/nbackup $U -L localhost:$D/$x.fdb
  $FB/bin/nbackup -F $D/$x.fdb
  rm -f $D/$x.fdb.delta; chown firebird:firebird $D/$x.fdb
  gfix -replica read_only localhost:$D/$x.fdb
  gfix -online normal localhost:$D/$x.fdb
  rm -f $D/$x.src/* $D/$x.src/.tmp
}

say "=== scenario $SC on $($FB/bin/isql -z 2>&1 | head -1)"
systemctl stop $SVC; rm -rf $D; mkdir -p $D/{a,b,c}.{jrn,arch,src}; chown -R firebird:firebird $D; : > $LOG
writeconf a=master; systemctl start $SVC; sleep 2
printf "create database 'localhost:%s/a.fdb' user SYSDBA password '%s';\ncreate table t(id bigint not null primary key, g int, v varchar(80));\nalter database enable publication;\nalter database include all to publication;\ncommit;\n" $D "$FB_PASSWORD" | $FB/bin/isql -q
load a 1 4
say "copy A -> B, C (nbackup -L, cp, -N, -F -SEQ)"
$FB/bin/nbackup $U -L localhost:$D/a.fdb
cp $D/a.fdb $D/b.fdb; cp $D/a.fdb $D/c.fdb; chown firebird:firebird $D/*.fdb
$FB/bin/nbackup $U -N localhost:$D/a.fdb
for x in b c; do $FB/bin/nbackup -F $D/$x.fdb -SEQUENCE; rm -f $D/$x.fdb.delta; chown firebird:firebird $D/$x.fdb; gfix -replica read_only localhost:$D/$x.fdb; done
G1=$(guid a); say "G1 = $G1"
writeconf a=master b=replica:$G1 c=replica:$G1; restart
load a 2 8
W=$(digest a); waitsame "$W" "b c" 60 a "b c" || exit 1

say "--- switchover: quiesce A"
sleep 3; ship a "b c"
gfix -shut full -force 0 localhost:$D/a.fdb
sleep 3
if [ $SC = r3 ]; then
  # hold the newest A segment back from C
  last=$(ls $D/a.arch | sort | tail -1); echo "$last" >> $D/ship.a.c; say "HOLD $last from c"
fi
ship a "b c"; sleep 1; ship a "b c"
waitsame "$W" "b" 30 a "b c" || exit 1
[ $SC = r3 ] || waitsame "$W" "c" 30 a "b c" || exit 1
say "S = $(ls $D/a.arch | sort | tail -1)"; hdr a; hdr b; hdr c

say "--- promote B (new GUID in place)"
gfix -shut single -force 0 localhost:$D/b.fdb
gfix -replica none localhost:$D/b.fdb
$FB/bin/nbackup $U -L localhost:$D/b.fdb
$FB/bin/nbackup -F $D/b.fdb; rm -f $D/b.fdb.delta; chown firebird:firebird $D/b.fdb
gfix -online normal localhost:$D/b.fdb
G2=$(guid b); say "G2 = $G2"

say "--- rebind A and C"
# A must not be opened under its master section: Firebird would start a new
# journal segment and set its REPLICATION_SEQUENCE to 1 (seen in the first run)
writeconf a=none b=master c=replica:$G1
gfix -online single localhost:$D/a.fdb
rebind a
case $SC in
  r1) say "r1: C NOT rebound (no nbackup -F); only source_guid=G2, G1 control file removed"
      rm -f $D/c.src/* ;;
  *)  rebind c ;;
esac
hdr a; hdr c

say "--- V-1: restart with c source_guid still G1, then rewrite conf without restart"
writeconf a=replica:$G2 b=master c=replica:$G1; restart
load b 3 4
SKIP=""; [ $SC = r2 ] && SKIP=c
W3=$(digest b)
waitsame "$W3" "a" 40 b "a c" $SKIP
sleep 5; say "c with stale source_guid: $(digest c) (want unchanged $W)"
grep -m2 -i "guid mismatch" $LOG | sed 's/^/  log: /'
writeconf a=replica:$G2 b=master c=replica:$G2
sleep 8; ship b "a c" $SKIP; sleep 4
say "V-1: c after conf rewrite WITHOUT restart: $(digest c)"
restart
load b 4 4
W4=$(digest b)
if waitsame "$W4" "a c" 60 b "a c" $SKIP; then say "RESULT $SC: A, B, C SAME"; else say "RESULT $SC: DIFFERENT"; fi
say "--- replication.log (key lines)"
grep -iE "resetting|missing|mismatch|older|zero sequence|error|no longer needed|waiting" $LOG | grep -v "^$" | tail -25 | sed 's/^/  /'
