#!/usr/bin/env bash
# Stage 0b of hqcluster-node docs/switchover-keep-replicas-plan.ru.md (question В-7),
# plain Firebird 4/5, no node. Same stand as 2026-11-20-replica-rebind.sh: three
# databases on one server, A master, B and C replicas, a shell "shipper".
# After the switchover A->B, C is re-bound in place (nbackup -F, source_guid=G2)
# and the script writes the replica control file for G2 with position 0 into
# C's journal_source_directory, as node would (fbheader.WriteReplicaControl).
# Usage: FB_PASSWORD=... 2026-11-20-replica-control-seed.sh c0|c1|c2
#   c1  control file G2:0; G2 segment 1 held back from C, 2.. shipped; then 1 delivered
#   c2  control file G2:0; normal shipping (no regression)
#   c0  like c1, but no control file (the stage 0 r2 case, for comparison)
# The script wipes /data and rewrites /opt/firebird/replication.conf: run it
# only on a clean temporary host.
set -u
SC=${1:?scenario}
case $SC in c0|c1|c2) ;; *) echo "scenario: c0|c1|c2" >&2; exit 2 ;; esac
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
# fd0: what fd 0 of the server process is. The SuperServer closes fd 0 at
# start (divorce_terminal), and Firebird's replication AutoFile never closes
# fd 0 ("if (m_handle) close"): a control file opened as fd 0 keeps its flock,
# and the next open of that control file waits forever (the replica stops
# applying, no log line, systemctl stop waits 90 s).
fd0() { local p; for p in $(pgrep -x firebird); do [ $(ls /proc/$p/task | wc -l) -gt 1 ] && echo "fd 0 of the server: $(readlink /proc/$p/fd/0 || echo closed)"; done; }
hung() { say "$(fd0)"; fd0 | grep -q '/{' && { say "HANG: a control file is held by fd 0 (Firebird AutoFile bug), setup not valid"; exit 3; }; exit 1; }

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

# put X FILE Y: copy one archived segment of X into Y's journal_source_directory
put() { cp $D/$1.arch/$2 $D/$3.src/.tmp && chown firebird:firebird $D/$3.src/.tmp && mv $D/$3.src/.tmp $D/$3.src/$2; }

# ship X "Y Z" [hold_first_for=Y]: copy archived segments of X older than 1 s;
# the first segment for Y is held back (its name in $D/held.X.Y) until release X Y
ship() {
  local x=$1 holdfor=${3:-}
  for f in $(ls $D/$x.arch 2>/dev/null | sort); do
    [ $(( $(date +%s) - $(stat -c %Y $D/$x.arch/$f) )) -ge 1 ] || continue
    for y in $2; do
      grep -qx "$f" $D/ship.$x.$y 2>/dev/null && continue
      echo "$f" >> $D/ship.$x.$y
      if [ "$y" = "$holdfor" ] && [ ! -e $D/held.$x.$y ]; then echo "$f" > $D/held.$x.$y; say "HOLD $f from $y"; continue; fi
      put $x $f $y
    done
  done
}
release() { local f; f=$(cat $D/held.$1.$2); say "RELEASE $f to $2"; put $1 $f $2; }

# writectl DIR GUID SEQUENCE OFFSET DB_SEQUENCE: Firebird 4/5 replica control
# file (DataV1, little endian, no active transactions), named "{GUID}"
writectl() {
  python3 - "$@" <<'EOF'
import os, struct, sys
d, g = sys.argv[1], sys.argv[2].strip('{}').upper()
seq, off, dbseq = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
# signature[10] | version u16 = 1 | txn_count u32 | sequence u64 | offset u32 | pad u32 | db_sequence u64
b = struct.pack('<10sHIQIIQ', b'FBREPLCTL', 1, 0, seq, off, 0, dbseq)
p = os.path.join(d, '{' + g + '}')
with open(p + '.tmp', 'wb') as f:
    f.write(b)
    f.flush()
    os.fsync(f.fileno())
os.rename(p + '.tmp', p)
EOF
  chown firebird:firebird "$1"/{*}; chmod 660 "$1"/{*}
}

# showctl DIR: decode every control file in DIR
showctl() {
  python3 - "$1" <<'EOF'
import glob, os, struct, sys
for p in sorted(glob.glob(os.path.join(sys.argv[1], '{*}'))):
    b = open(p, 'rb').read()
    sig, ver, n, seq, off, _, dbseq = struct.unpack('<10sHIQIIQ', b[:40])
    print('  ctl %s: %d bytes, version %d, sequence %d, offset %d, db_sequence %d, active %d'
          % (os.path.basename(p), len(b), ver, seq, off, dbseq, n))
EOF
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

# waitsame M "Y Z" secs [ship X "Y Z" hold]: wait until digest(Y) = expected
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

say "=== scenario $SC on $($FB/bin/isql -z 2>&1 | head -1)"
systemctl kill -s KILL $SVC 2>/dev/null; systemctl stop $SVC; rm -rf $D; mkdir -p $D/{a,b,c}.{jrn,arch,src}; chown -R firebird:firebird $D; : > $LOG
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
W=$(digest a); waitsame "$W" "b c" 60 a "b c" || hung

say "--- switchover: quiesce A"
sleep 3; ship a "b c"
gfix -shut full -force 0 localhost:$D/a.fdb
sleep 3; ship a "b c"; sleep 1; ship a "b c"
waitsame "$W" "b c" 30 a "b c" || hung
say "S = $(ls $D/a.arch | sort | tail -1)"; hdr b; hdr c; showctl $D/c.src

say "--- promote B (new GUID in place)"
gfix -shut single -force 0 localhost:$D/b.fdb
gfix -replica none localhost:$D/b.fdb
$FB/bin/nbackup $U -L localhost:$D/b.fdb
$FB/bin/nbackup -F $D/b.fdb; rm -f $D/b.fdb.delta; chown firebird:firebird $D/b.fdb
gfix -online normal localhost:$D/b.fdb
G2=$(guid b); say "G2 = $G2"
writeconf a=none b=master c=replica:$G1

say "--- re-bind C (plan §3, steps 3-11)"
gfix -shut single -force 0 localhost:$D/c.fdb
gfix -replica none localhost:$D/c.fdb
$FB/bin/nbackup $U -L localhost:$D/c.fdb
$FB/bin/nbackup -F $D/c.fdb; rm -f $D/c.fdb.delta; chown firebird:firebird $D/c.fdb
gfix -replica read_only localhost:$D/c.fdb
rm -f $D/c.src/* $D/c.src/.tmp $D/c.src/{*}
if [ $SC = c0 ]; then
  say "c0: no control file"
else
  writectl $D/c.src "$G2" 0 0 0
  say "control file written for G2: sequence 0, offset 0, db_sequence 0"
fi
showctl $D/c.src
gfix -online normal localhost:$D/c.fdb
hdr c
writeconf a=none b=master c=replica:$G2; restart

HOLD=""; [ $SC = c2 ] || HOLD=c
load b 3 6
W3=$(digest b)
if [ -n "$HOLD" ]; then
  say "--- G2 segment 1 held back from C, segments 2.. shipped"
  for i in $(seq 1 25); do ship b c $HOLD; sleep 1; done
  say "C while segment 1 is held: $(digest c) (unchanged would be $W, B is $W3)"
  say "C mailbox: $(ls $D/c.src | tr '\n' ' ')"; showctl $D/c.src
  awk '/^[^ \t]/{ts=$6} /Database: \/data\/c/{c=1;next} /Database:/{c=0} c && /VERBOSE|ERROR/{print ts, $0}' $LOG | tail -6 | sed 's/^/  log C: /'
  say "$(fd0)"
  release b c
fi
waitsame "$W3" "c" 60 b c $HOLD
load b 4 4
W4=$(digest b)
if waitsame "$W4" "c" 60 b c $HOLD; then say "RESULT $SC: B, C SAME"; else say "RESULT $SC: DIFFERENT"; fi
showctl $D/c.src; hdr c; say "$(fd0)"
say "--- replication.log (key lines)"
grep -iE "resetting|missing|mismatch|older|zero sequence|error|no longer needed|waiting|is scanned" $LOG | tail -25 | sed 's/^/  /'
