# Tests

`python tb.py test list` shows them. Every test writes
`state/results/<test>-<time>/results.json` and `summary.md` and exits 0 only
when every case passed.

Every "converge" step has three checks. First the rows: every replica must
match the master (row counts of keyed tables). Then, once the rows match, the
replica control files: with the load stopped, Firebird's control file
(`{GUID}` in the replica's journal source folder) must hold no active
transaction within 90 s. A transaction left there keeps the replica's OAT and
every segment after it, and Firebird replays it again after its next restart
(hqcluster-node N-14: a reinit replay that lists transactions already ended).
Last, the replica records: each replica must hold one record per database
file in `GET /v1/databases` (hqcluster-node N-12: a scan before a reinit left
a second record beside the reinit's).

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

## Not ported yet

From `hqcluster-node/examples`, still to move here as test bed tests:

- transfer faults with a `-tags faultinject` master (`fault-transfer`);
- synthetic segment pushes (checksum, duplicate, gap, sequence conflict,
  mailbox ceiling, free-space floor) from `live-windows-pair/30-disasters.ps1`;
- database file safety checks (`50-db-file-safety.ps1`, `dbfile-safety.py`).
