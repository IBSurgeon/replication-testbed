# Tests

`python tb.py test list` shows them. Every test writes
`state/results/<test>-<time>/results.json` and `summary.md` and exits 0 only
when every case passed.

Every "converge" step has four checks. First the rows: every replica must
match the master (row counts of keyed tables). Then, once the rows match, the
replica control files: with the load stopped, Firebird's control file
(`{GUID}` in the replica's journal source folder) must hold no active
transaction within 90 s. A transaction left there keeps the replica's OAT and
every segment after it, and Firebird replays it again after its next restart
(hqcluster-node N-14: a reinit replay that lists transactions already ended).
Last, the replica records: each replica must hold one record per database
file in `GET /v1/databases` (hqcluster-node N-12: a scan before a reinit left
a second record beside the reinit's).
Then the applied segments the master sees: within 120 s its ledger
(`GET /v1/transfer`) must show every replica's `last_applied` at its
`last_acked`. The master learns it by polling the replica; a poll that fails
(a certificate that does not match, 2026-09-27) leaves it behind for ever.

## basic

Load for `--minutes` (default 3), stop, then every replica must match the
master (row counts of keyed tables).

```bash
python tb.py test basic --minutes 10 --load-mode mixed --tx emul-safe
```

## reinit_cycles

Many reinits under load. A reinit locks the master database with nbackup
(`-L`), copies it to the replica, unlocks it, and the replica applies the
journal from that point.

Per reinit:

- the operation succeeds (a smooth reinit refused because of long
  transactions is retried every 30 s, up to `--refusal-timeout`);
- no `<db>.delta` is left on the master;
- the replica's database generation goes up.

After all cycles: no limbo transactions on the master, and every replica
matches the master.

```bash
python tb.py test reinit_cycles --cycles 10 --modes standard,smooth
python tb.py test reinit_cycles --cycles 5 --db db1 --replicas replica1 --tx full
python tb.py test reinit_cycles --cycles 5 --parallel     # all databases at once; the node queues them
```

## disasters

Each scenario runs under load; after it the load stops and every replica must
catch up. Each scenario restores the host in a `finally` block.

| Scenario | What happens | Extra check |
|---|---|---|
| `replica-node-stop` | replica node service stopped for `--down` s | node answers again |
| `replica-node-kill` | replica node process killed | the service manager restarts it |
| `master-node-stop` | master node service stopped | node answers again |
| `both-nodes-stop` | both node services stopped | both answer again |
| `replica-fb-stop` | Firebird on the replica stopped | master ledger shows acked > applied while it is down |
| `master-fb-restart` | Firebird on the master restarted through the node API | restart accepted, node answers again |
| `partition` | the replica host blocks the master address on its node port (firewall) | rules removed |

```bash
python tb.py test disasters --only partition,replica-fb-stop --down 120
```

## datacheck

The node's periodic data check (hqcluster-node N-09). The test turns it on
through `PUT /v1/config` on every node (interval `--interval`, default 60 s;
quiet wait `--quiet`, default 30 s; the keyed tables as `sync_tables`) and
restarts the nodes. It runs load for `--minutes` (default 4), stops, and
waits for the rows to converge. Then:

- every node has a history (`GET /v1/databases/{db_id}/datacheck`) with at
  least one `ok` sample;
- the master's comparisons hold no `mismatch`: a pair taken under load must
  be `not_comparable`;
- within `--match-timeout` (default 600 s) every replica has a `match`;
- no `data_mismatch` alert on the master.

At the end the check is turned off again and the nodes restarted.

```bash
python tb.py test datacheck --minutes 4
```

## states

Every state of one database and the transitions between them, T1-T36 of
the state machine (hqbirdrcm `docs/REPLICATION_STATE_MACHINE.md`, section
2.3). The test makes its own database (a copy of EMPLOYEE in `--subdir`,
default `tbsm`), drives it through 18 scenarios and removes it at the end
(`--keep` keeps it).

How a transition is seen: the node journals every state change of a
database in `journal.jsonl` next to node.json before it acts on it.
`90-hostctl statelog` reads those changes from a mark on, so the test sees
every state, short ones too. A case such as `T10 IN_SYNC->LAGGING [pause,
master]` passes when the two states follow each other in the journal; the
note shows the whole chain.

| Scenario | Transitions | How |
|---|---|---|
| create | T1 T3 T6 T7 T8 T29 T30, replica T32 | `dbs prepare` of one database: scan, Firebird restart, publication, reinit |
| pause, replica-lag, blocked, backpressure | T9 T10 T11 T12 T13 T14 | paused peer; Firebird stopped on the replica; a segment removed from its mailbox; mailbox ceiling 2 |
| apply-errors, master-stop, disabled, constraint | T15 T16 T17 T21 T32 T33 | ERROR blocks appended to replication.log (simulated, see below) |
| conflict, foreign | T20 T23 | a segment pushed to the replica from the master host with the master's certificate: the last sequence with other bytes; another database's segment |
| replaced, segment-lost, disk | T24 T25 T27 | master file with a new GUID (nbackup copy, `-F`); a segment gone from the mailbox and the archive; free-space floor above the free space |
| reinit-fail, stale | T29 T31, T7 from FAILED, T26 | reinit to a stopped replica node (FAILED), then publication/sync; the master node stopped right after it released the nbackup lock (stale generation) |
| orphan, remove | T34 T35 T4 T6 T36 | the file in `exclude_paths` and back; `dbs remove` and forget |

Firebird cannot be made to log an apply error, "Replication is stopped",
"disabled" or a key violation on demand, so the test appends such blocks to
replication.log in Firebird's format: the node reads that file for what
Firebird did. The case note says "simulated".

The `disk` scenario raises the master's free-space floor for about a
minute: the retention ladder then runs for every database of the master.
Run it with the other test databases idle.

Not driven: T2 and T5 (takeover of a replication set up by hand), T18/T19 (a
segment damaged in transit), T22 (a foreign file in the mailbox as a sign of
a replaced master). T28 (retention by API) is not told apart from T27.

One more case after all scenarios: the master journal has no hop
`IN_SYNC > CONFIGURED > IN_SYNC` or `FAILED > IN_SYNC > FAILED` inside a
scenario (a master tick without a full verdict made both, hqcluster-node C3).

```bash
python tb.py test states                         # all scenarios, about 1.5 hours
python tb.py test states --only create,pause,blocked --keep
```

## gaps

Section 12 of the same document lists eight possible gaps. The test makes
each one happen and checks what an operator needs. PASS: not a real
problem. FAIL with "confirmed: ...": the problem is real, the note says what
was seen. A "setup" FAIL means the situation could not be made.

| Item | Case | What the test does |
|---|---|---|
| 1 conflict | `sequence_conflict` clears after hold/release | conflict pushed to the replica, hold released, load; the master must leave `NEEDS_ATTENTION` within `--settle` (300 s) |
| 2 disabled | DISABLED clears once segments apply again | simulated "disabled", then load; the replica must leave `DISABLED` |
| 3 failed | FAILED stops shipping to the other replicas | reinit to a stopped replica node fails; the second replica must still get segments |
| 4 frozen | stale_generation freezes the others | master stopped after the lock release (replica has the new generation, the master rolls back), publication sync; after `NEEDS_REINIT` the second replica must still get segments |
| 5, 6 crash | lock, SEEDING, next reinit after a crash | master node killed while the reinit holds the nbackup lock: no `.delta` left, `SEEDING` leaves, the next reinit succeeds; the replica's abandoned session does not block it |
| 7 rcm-jobs | RCM jobs after an RCM restart | a verify job and a command, RCM restarted right after each; both must end, not stay running |
| 8 rcm-disabled | DISABLED is visible in RCM | simulated "disabled"; RCM must show its `replication_disabled` alert (another alert of the database does not count) |
| 5b crash-unlocked | operator backup keeps its lock after a crash past the unlock | master node killed 3 s after `nbackup -N` (the reinit waits for the replica); after the start an operator `nbackup -L` must still hold 150 s later |
| 5c second-restart | lock released after a second restart | master killed under the lock and kept down; `nbackup` renamed away (`fb-tool off`), the node started (its unlock fails) and restarted again; once `nbackup` is back the `.delta` must go. Firebird merely stopped is not enough: `nbackup -N` still worked |
| 4b two-stale | a reinit to one stale replica keeps the other one NEEDS_REINIT | both replicas made stale (as item 4); a reinit to the first: the master stays `NEEDS_REINIT` for the second, which gets no segments; after a node restart too |
| C5 generation | replica generation after a reinit of an IN_SYNC replica | the replica's generation in its API must be the master's new one |

Items 5b, 5c, 4b and C5 come from the review of the v2 merge
(hqcluster-node `docs/v2-gaps-fix-plan.md`). "recover after ..." checks the
replicas too: each must be `IN_SYNC`, not only the master.

Items 3, 4 and 4b need two replicas. Items 7 and 8 need the RCM login
(`secrets.rcm_user`, `secrets.rcm_password`); without it they are SKIP. After
every item the test reinits the database until everything is `IN_SYNC`.

```bash
python tb.py test gaps
python tb.py test gaps --only conflict,disabled --settle 600
```

## upgrade

A 2027.1.x node upgraded to this build over its own state (schema 1). Run
it on a fresh install or last: it removes the node and its state on every
host. It installs the old node from `--old-dist` and makes two test
databases with it; on the old version db2 becomes `NEEDS_ATTENTION` on the
master (simulated "Replication is stopped"), a replica of db1 `NEEDS_REINIT`
(3 simulated key violations), and the master is killed under a reinit's
nbackup lock of db1 and kept down (`node-on-file --action kill-stay`: systemd
would start the old node again, and it would release the lock itself).
Then this build is installed over it. Cases: the node opens the old state
(schema 2001); this build releases the old node's lock; db1 leaves
`SEEDING`; db2 stays `NEEDS_ATTENTION/replication_log_error` over a minute
of reconcile ticks; the replica stays `NEEDS_REINIT/upgrade_needs_reinit`;
a reinit brings db1 to `IN_SYNC`. No load: fb-loadgen is not needed.

```bash
python tb.py test upgrade --old-dist state/dist-2027.1.15
```

## engineparams

Engine replication keys (`GET/POST /v1/engine/params`) on every engine: the
keys come from the node's catalog (4/5: `journal_*`, 2.5/3.0: `log_*`). On
the master a value for one database (dry run first), no Firebird restart by
the node, a restart through the API, the node default for the others, two
scans in a row (the second changes nothing). On each replica a node default
and a restart in its window; a 2.5/3.0 replica has no editable key yet
(V-22: SKIP); a 4.0 replica has no `cascade_replication`. Every change is
undone.

## legacy

HQbird 2.5/3.0 through the replconf plugin; `firebird.engine` 2.5 or 3.0 on
every node host, Linux hosts for now. `dbs prepare` already activated the
plugin on every node (`POST /v1/replconf/activate`: the node's plugin
2.1.0, `replconf.properties`, one restart). Steps (`--steps`):

| Step | What |
|---|---|
| `activation` | the node's file is its default `<root>/replconf.hqcluster.hqbird`, `replconf.properties` points to it, plugin 2.1.0; the files the engine uses and its systemd unit are recorded |
| `publications` | `POST /v1/publication/sync` answers 409 `no_publications` |
| `flow` | load, stop, every replica matches |
| `restart` | the master's Firebird restarted during the load |
| `busy_writers` | a reinit while a transaction that has written stays open (`hold-tx`) fails with `reinit_busy_writers`; once it is gone the reinit goes through |
| `properties` | a foreign edit of `replconf.properties` (to a copy of the node's file) raises `replconf_properties_changed`; the node does not repair it; restored at the end |
| `valid_date` | `firebird.replconf_valid_till` 20 days ahead raises `replconf_expiring`; the old date comes back (never a past one: HQbird then refuses every attach) |
| `turn_to_normal` | a replica database turned to normal, then seeded again |

```bash
python tb.py test legacy
python tb.py test legacy --steps activation,flow
```

## rcmtable

The database tables of the RCM **Databases** page, read as the page reads
them: a web login (`90-hostctl rcm-web`, form login and session cookie, like
a browser) and `/partials/db-table?tab=m1|m2|m3`. m1 is the master-oriented
table, m2 the replica-oriented one, m3 the combined one (a block per master
database with a row per replica, "not paired" rows with Initialize, a master
checkbox; hqcluster3 `docs/RCM_UI_DB_TABLE_MERGE_PLAN.md`). Needs the RCM login
(`secrets.rcm_user` / `rcm_password`). Load is stopped; the test ends with a
converge of every test database. Cases (`--cases`, in this order):

| Case | What |
|---|---|
| `table` | m3 and m2: a block per test database, a row per replica, nothing else; m1: a column per replica node |
| `alien` | "alien clean off" on one replica (RCM `drop-foreign`) marks every row of the block in m2 and m3, so a filter hides the whole block, never only the row with the master's cells; switched back at the end |
| `dup` | a second replica of one database on one node: the replica file copied into `<db_root>/tb-copy/...` with Firebird stopped, a scan takes it; m3 shows two rows of one block (m1: a second column for the node). At the end the copy is deleted, then removed from management (Linux replica) |
| `peer` | the last replica loses its test database files (deleted with Firebird stopped), then RCM takes them out of its management (`unmanage`): the replica holds nothing of the master but is its peer, so every m3 block has a "not paired" row with Initialize to it (m1 has no column for it at all); Initialize from RCM (`POST /v1/nodes/{master}/reinit`, the button's request) brings each database back, one block at a time (Linux replica) |

"Remove from management" of a replica whose file is still on disk is
refused by the node (`mailbox_active`: its mailbox is how segments reach
the file), after RCM has already added the file to `exclude_paths`. That is
why `dup` and `peer` delete the files first.

```bash
python tb.py test rcmtable
python tb.py test rcmtable --cases table,dup
```

## promote

"Promote to master" through RCM, as the Databases page's button does: a
replica's database is handed to the **companion**, a master-role node next to
the replica node on the same Firebird (`hosts.<replica>.companion` in the
config; `install --source local` puts it in `<paths.node>/companion`, RCM
pairs the two by one `host` label). Needs the RCM login. The last test
database (or `--db`) of the first replica with a companion (or `--replica`):

| Check | What |
|---|---|
| setup | RCM pairs the companion with the replica (role master, one `host` label in `/v1/rcm/config`); no `topology_mismatch` alert; m3 offers Promote on the donor's row |
| promote | `POST /v1/nodes/{donor}/databases/{db}/promotetomaster`; the job (`GET /v1/promotes/{id}`) ends `ok`, every step done |
| companion | the companion reports the file as its database |
| donor | the file is in the replica node's `databases.exclude_paths`; its record is at most ORPHANED (RCM hides it behind the Ghost row) |
| writable | the promoted database takes a write (`90-hostctl write-probe`) |
| old master | not Degraded and not paused (it keeps its other replica); a minute of load reaches that replica |
| rcm view | a Ghost row of the donor under the old master (`/v1/databases` and m3); a block of the new master, with no replica row of the old master's replicas; `duplicate_guid` raised (two masters, one GUID) |
| clear | Clear removes the Ghost row; m3 then shows the donor's node as "not paired" for the old master, not its ORPHANED record as a replica row |
| guard | Initialize from that row (old master to the donor's node) must not overwrite the promoted database: RCM or the node refuses, and the new master still takes writes |

The promoted database stays a master: run this test last, or remove and
prepare the databases again.

```bash
python tb.py test promote
```

## guidpromote

A promote that ends with a GUID of the new master's own, the way the planned
RCM promote step `new_guid` will do it, but through the **running** Firebird
service and **under load**. RCM promotes the last test database not promoted
yet (or `--db`) from the first replica with a companion (as `promote` does);
then on that host, through the server: replica mode off, `gfix -shut single
-force 0`, `nbackup -L`, `nbackup -F` without `-SEQUENCE`, the delta removed,
`gfix -v -full`, `gfix -online` (`90-hostctl guid-promote`; `--mode stop`
stops Firebird instead of the shutdown).

| Check | What |
|---|---|
| before | the promoted database still has the old master's GUID (reinit copies with `-SEQUENCE`); RCM groups the two masters as a duplicate GUID |
| new guid | every step returns 0; a new GUID; sequence 0 right after `-F` (a publishing master opens segment 1 once online); no replica mode, lock or shutdown; validation clean; the delta made and removed; Firebird not restarted (shutdown mode) |
| journal | the new master's segments carry the new GUID |
| rcm | the new master's group is no duplicate any more; its m3 block holds no replica of the old master |
| initialize | RCM Initialize from the new master to another replica is not refused; with `--minutes` of load on the new master the replica ends equal |
| guard | Initialize from the old master onto the promoted file is refused (now by the node: the file is no replica) and the file keeps its GUID and takes writes |
| converge | the old master and its other replica; every other database on the promoting host (the same Firebird kept applying them) |

The companion node already manages the file when its GUID changes, so it
reports it (`master_db_replaced`, journal quarantine); the test notes these:
the planned step runs before the companion enrolls the file. The promoted
database stays a master. Linux hosts only.

```bash
python tb.py test guidpromote [--db db9] [--mode shutdown|stop] [--minutes 2]
```

## guidprobe

Any engine; made for HQbird 2.5/3.0, where a replica names its master in its
own header (`Replication master GUID`, written by `gfix -replica {GUID}`).

| Check | What |
|---|---|
| pairs | per test database and replica (`gstat -h`, `90-hostctl db-header`): the replica's Database GUID against the master's; on 2.5/3.0 its Replication master GUID must equal the master's Database GUID |
| rcm | RCM puts each replica in its master's group (`/v1/databases`), none in a group without a master |
| scratch | a copy of the master made as reinit makes a replica (`90-hostctl db-copy-locked --fixup seq --replica …`) keeps the master's GUID: promoted as it is, it would publish under that GUID |
| new guid | the in-place change on that copy through the running service (`guid-promote`; replica mode off is `-replica {}` on 2.5/3.0): new GUID, sequence 0 after `-F`, no replica mode, no master GUID, validation clean, Firebird not restarted, the copy takes a write |

The copy is `<db>.tbguidprobe` beside the master database (not `*.fdb`, so the
node does not take it) and is removed at the end (`--keep` keeps it). Linux
hosts only.

```bash
python tb.py test guidprobe
```

## noseq

A replica recreated by hand without `-SEQUENCE` over a working replication,
as the HQbird guide's `nbackup -f` without `-seq` makes one. Firebird 4.0 then
skips the new segments without an error, 5.0.4+ stops with "zero sequence
number". The test looks for a sign the node could watch without
`verbose_logging`: the replica's replication sequence is 0 while the control
file `{master GUID}` in its journal source folder holds `db_sequence` > 0.

| Check | What |
|---|---|
| baseline | a fresh reinit and a short load: the replica's sequence N > 0 and its control file's `db_sequence` = N |
| recreate | the master copied under nbackup lock, `-F` without `-SEQUENCE`, `gfix -replica read_only`, put in place of the replica with Firebird stopped (`db-copy-locked --fixup noseq`, `replace-db`) |
| sign | replica sequence 0 while the control file holds `db_sequence` > 0; after the load it is still there (the control file's `sequence` moves on, `db_sequence` does not) |
| firebird | rows on the replica after `--seconds` of load and the replication.log lines about it (noted, not failed) |
| node | the node state or RCM flags the replica; FAIL when a replica that lost rows still looks healthy. Alerts are noted only: `db_file_replaced` says the file changed, not that segments are lost |
| restore | reinit and converge |

By default the last test database that is still a replica on the host
(`guidpromote` leaves a promoted file). Firebird 4.0/5.0 only; Linux hosts only.
The copy goes through this machine (scp), so keep the database small.

```bash
python tb.py test noseq [--db db3] [--replica replica1] [--seconds 90]
```

## replconfdate

HQbird 2.5/3.0: the valid date of the node's replconf file (hqcluster-node
`docs/replconf-valid-date-plan.md`, checks С-1..С-6). The date comes only from
`firebird.replconf_valid_till` in node.json. An empty key gets the node's
first start + 30 days; fbagent writes the licence date over it later.

Needs a fresh `install` (no `replconf_valid_till` in node.json) and
`dbs prepare` (it activates the plugin). Linux hosts only.

| Step | Plan | What |
|---|---|---|
| default | С-1 | every node: node.json has the node's first start + 30 days (journal event `replconf_default_valid_till`), `valid_till_source: default`, the node's file holds the date, `replconf_expiring` is `info`; a node restart keeps the date |
| first_start | С-6 | `firebird.log` "Valid date" lines of the fresh install and the file the engine read before the activation (noted) |
| activation | С-2 | every node: plugin 2.1.0, `replconf.properties` names the node's file; a Firebird restart and an attach add no "Valid date" line |
| auto_activate | С-2 | `replconf.properties` names a missing file: critical `replconf_properties_changed`; a replica switches back by itself at its next start; the master with Firebird running is not switched (critical alert). Firebird stopped and the node restarted (its unit starts Firebird again): HQbird 3.0 stops on the broken file and the node, checking each minute, switches it; HQbird 2.5 keeps running under fbguard (it starts the server again after each refused attach): the master keeps the critical alert, or is switched when the node finds the port closed twice; both are right |
| config_date | С-3 | what fbagent does: a new date written into node.json with the node stopped. At start the node's file has it, source `config`, no `replconf_expiring`. A past date is not written (`replconf_not_ready`). Then the default back |
| expiry | С-4 | the replica's clock moved: 6 days before the default date `replconf_expiring` is `warn`; on the date `replconf_expired` is `critical` (attaches noted); clock back: cleared, attach works |
| old_plugin | С-5, С-6 | on the replica, the engine as before the activation: the plugin HQbird installs (before 2.1.0, the copy the activation kept) and a DataGuard file in v2.0, as DataGuard writes it on Linux. 2099-12-29 and 2028-01-31 work today and 2099-12-29 works on the 30th: the plugin compares whole dates. The v1.2 template HQbird installs (`/opt/hqbird/conf/replconf.hqbird.empty`) is refused: this plugin reads no v1.2 (V-12). The node raises a critical alert, the replica switches to its file at start, attaches work |

At the end every test database must converge. The clock steps set the
replica's clock with time sync off and put it back from this machine's clock
(`hostctl clock`). С-3 does not run fbagent and goafts: the fbagent side is
covered by its unit tests (`internal/clusterprov`).

```bash
python tb.py test replconfdate [--steps default,activation] [--replica replica1]
```

## Not ported yet

From `hqcluster-node/examples`, still to move here as test bed tests:

- transfer faults with a `-tags faultinject` master (`fault-transfer`);
- synthetic segment pushes (checksum, duplicate, gap) from
  `live-windows-pair/30-disasters.ps1` (sequence conflict, mailbox ceiling
  and free-space floor are in `states`);
- database file safety checks (`50-db-file-safety.ps1`, `dbfile-safety.py`).
