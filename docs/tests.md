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
| 5c second-restart | lock released after a second restart | master killed under the lock; Firebird stopped, the node started (its unlock fails) and restarted again; once Firebird is back the `.delta` must go |
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

A 2027.1.x node upgraded to this build over its own state (schema 1), with
an interrupted reinit and a `NEEDS_REINIT` replica. Run it right after
`install`, before `dbs prepare`: it removes the node and its state on every
host, installs the old node from `--old-dist`, makes a test database with
it, kills the master under a reinit's nbackup lock and makes a replica
`NEEDS_REINIT` (3 simulated key violations), then installs this build over
it. Cases: the node opens the old state (schema 2001); the lock is released;
the master leaves `SEEDING`; the replica stays
`NEEDS_REINIT/upgrade_needs_reinit` after a minute of load; a reinit brings
everything to `IN_SYNC`.

```bash
python tb.py test upgrade --old-dist state/dist-2027.1.15
```

## Not ported yet

From `hqcluster-node/examples`, still to move here as test bed tests:

- transfer faults with a `-tags faultinject` master (`fault-transfer`);
- synthetic segment pushes (checksum, duplicate, gap) from
  `live-windows-pair/30-disasters.ps1` (sequence conflict, mailbox ceiling
  and free-space floor are in `states`);
- database file safety checks (`50-db-file-safety.ps1`, `dbfile-safety.py`).
