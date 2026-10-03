# Replication test bed

Scripts that build a Firebird replication cluster (hqclusternode + fbagent +
hqbirdrcm) on real Linux and Windows hosts, put load on it, and test it:
many reinits under load, failures, network partitions.

- **Modules** (`modules/linux/*.sh`, `modules/windows/*.ps1`) run ON a host.
  Each module does one job and has a matching "remove" step.
- **`tb.py`** runs on the operator machine. It reads the local config, copies
  the modules to every host over ssh, runs them in the right order, and drives
  the nodes through their API.
- **Tests** (`tests/*.py`) are `tb.py test <name>` commands.

Default cluster: one master and **two replicas on different hosts**, RCM on the
master host. Any host can be Linux or Windows.

## No secrets in this repository

Real host names, addresses, users, URLs, pins and passwords live **only** in
`config/testbed.local.json` on the operator machine. That file is git-ignored.
The repository holds `config/testbed.example.json` with `<placeholders>`.
Run state (`state/`: generated tokens, certificates, rendered configs, results)
is git-ignored too.

Before a push, run `python tools/secret_scan.py`, or enable the hook once:

```bash
git config core.hooksPath tools/hooks
```

## Requirements

| Where | What |
|---|---|
| Operator machine | Python 3.8+, OpenSSH client (`ssh`, `scp`), `curl`; Go and git for building fb-loadgen |
| Linux host | root over ssh (or `sudo: true`), systemd, `python3`, `curl`, Firebird/HQbird installed (`/opt/firebird`) |
| Windows host | OpenSSH Server, an administrator account, Windows PowerShell 5.1, Firebird/HQbird installed as a service |
| Network | node ports (default 7051/7052) open between master and replicas; 7443 open to the RCM host |
| DigitalOcean droplets (`tb.py do create`) | **4 GB droplets**: `digitalocean.size` = `s-2vcpu-4gb`. Use this size for every stand |

## Quick start

```bash
cp config/testbed.example.json config/testbed.local.json   # then fill it in
python tb.py check
python tb.py install --source local          # or: --source goafts
python tb.py dbs prepare --count 2           # master databases + first copy to each replica
python tb.py loadgen deploy --from git       # build, copy to the master, 5 s smoke run
python tb.py test basic
python tb.py test reinit_cycles --cycles 10
python tb.py test disasters
python tb.py test datacheck
python tb.py test states                     # every state and transition of one database
python tb.py test gaps                       # section 12 gaps of the state machine: real or not
python tb.py test engineparams               # engine keys: per database, node default, restart rules
python tb.py test legacy                     # HQbird 2.5/3.0: replconf plugin, flow, reinit, properties, valid date
python tb.py test rcmtable                   # RCM Databases page tables (m1/m2/m3): not paired + Initialize, two replicas on one node
python tb.py test promote                    # Promote to master to the companion node (hosts.<replica>.companion); leaves db2 promoted
python tb.py test guidpromote                # promote, then a new GUID in place through the running service, under load
python tb.py test guidprobe                  # GUIDs of the pairs, RCM pairing, in-place GUID change on a scratch copy (any engine)
python tb.py test noseq                      # a replica recreated by hand without -SEQUENCE: the sign and who notices (4.0/5.0)
python tb.py test replconfdate               # HQbird 2.5/3.0: the replconf valid date (default, auto-activation, fbagent date, expiry, old plugin); moves a replica's clock
python tb.py test upgrade --old-dist DIR     # 2027.1.x -> this build over its state (fresh install or last)
python tb.py test pubtables                  # node 2027.4.4: publication per table, on/off, syncs under load (4.0/5.0)
python tb.py test replicakeep                # node 2027.4.4: a replica keeps its reinit copies with recursive=false
python tb.py test reinitroot                 # node 2027.4.4: a missing databases root named and created (prepare)
python tb.py test restartwait                # node 2027.4.4: the wait for the replica's restart; no extra restart
python tb.py test guidsigns                  # node 2027.4.5: master_guid of a replica; replica_control_missing
python tb.py test tracegate [--repro]        # HQbird 3.0 replica: no fbagent trace session; --repro: the engine abort by hand
python tb.py dbs remove
python tb.py uninstall --source local        # fails with a list if anything is left
python tb.py hosts wipe --hosts all --yes    # remove all the test bed put on the hosts
```

## Modules and commands

| # | Module | tb.py command | What it does |
|---|---|---|---|
| 1 | `10-local` | `install --source local` / `uninstall --source local` | fbagent, hqclusternode, hqbirdrcm from local copies of the binaries; no goafts enrollment |
| 2 | `20-goafts` | `install --source goafts` / `uninstall --source goafts [--deregister]` | everything from a goafts server (bootstrap URL + SPKI pin); fbagent is enrolled |
| 2a | `20-goafts` | `goafts up` / `goafts down --yes` / `goafts date` / `goafts channel` / `goafts status` | a stand that goafts manages: the agents enroll, goafts puts the hosts in one cluster (`goafts.cluster` in the config), and each agent writes node.json / rcm.json and installs node and RCM itself - the customer's chain; the tests then run as on any stand |
| 3 | `30-dbs` | `dbs prepare [--count 2] [--subdir tb]` / `dbs remove` | N copies of EMPLOYEE on the master (nbackup -L copy, -F), scansync, Firebird restart, publication, first reinit to every replica |
| 4 | `40-loadgen` | `loadgen deploy --from git\|local\|binary [--target master\|local]` | builds fb-loadgen, puts it on the master (or keeps it on this machine), 5 s smoke run |
| 5 | `50-load` | `load start\|stop\|status` | load on one, several or all test databases; write / read / mixed / spike / oltp-emul; with or without changing transactions |
| 6 | `10-local`, `20-goafts` | `install --hosts replicas` | replicas are installed the same way as the master |
| 7 | test | `test reinit_cycles` | many reinits (nbackup -L on the master) under load, one or more databases, smooth and standard |
| 8 | test | `test disasters` | node stop/kill, Firebird stop/restart, network partition, under load |
| 9 | test | `test datacheck` | the node's periodic data check (N-09): history on every node, no mismatch under load, a match once quiet |
| 10 | test | `test states` | every state and transition (T1-T36) of one database, read from the node's state journal |
| 11 | test | `test gaps` | the eight possible gaps of the state machine (its section 12): real problem or not |
| 12 | test | `test upgrade` | a 2027.1.x node upgraded to this build over its state: migration, an interrupted reinit's lock |
| 13 | test | `test engineparams` | engine replication keys from the node's catalog: per database, node default, restart rules |
| 14 | test | `test legacy` | HQbird 2.5/3.0 through the replconf plugin: activation, flow, restart, reinit without replay, properties, valid date, turn to normal |
| 15 | test | `test rcmtable` | the RCM Databases page tables (m1/m2/m3) read through a web login: rows per replica, alien-off on a whole block, two replicas of one database on one node, a peer with no database ("not paired" + Initialize) |
| 16 | test | `test promote` | "Promote to master" to a companion node on the replica's Firebird: job steps, both nodes, writes, Ghost row, duplicate GUID, Clear, and Initialize refused over the promoted file |
| 17 | test | `test guidpromote` | promote, then a GUID of its own in place through the running Firebird (single shutdown, `nbackup -L`, `-F` without `-SEQUENCE`), under load: new GUID, no duplicate GUID in RCM, Initialize from the new master, guard |
| 18 | test | `test guidprobe` | the GUIDs of each master/replica pair (HQbird 2.5/3.0: Replication master GUID), RCM pairing, and the in-place GUID change on a scratch replica copy |
| 19 | test | `test noseq` | a replica recreated by hand without `-SEQUENCE` over a working replication: replica sequence 0 with control file `db_sequence` > 0, what Firebird does, whether the node notices |
| 21 | test | `test pubtables` | node 2027.4.4: tables out of the publication and back, publication off and on (info alert), ten syncs and two real changes under load, verify with a table out (`not_published`) |
| 22 | test | `test replicakeep` | node 2027.4.4: a replica with `recursive: false` keeps the copies reinit placed in `<root>/<master>/`, through a scan and a node restart, and still converges |
| 23 | test | `test reinitroot` | node 2027.4.4: a missing databases root fails the permissions check with the fix, `POST /v1/databases/root/prepare` creates it 2770 with Firebird's group |
| 25 | test | `test guidsigns` | node 2027.4.5: `GET /guid` and the stats row of a replica name its master (`master_guid`, `master_guid_from`, `control_guid`, `source_node_id`/`source_db_id`); the control file removed from a working 4.0/5.0 replica raises `replica_control_missing` and NEEDS_REINIT (Firebird 5.0 then skips segments) |
| 26 | test | `test tracegate` | HQbird 3.0 replica: fbagent 2.59+ runs no trace session there; the replica applies under load without an abort; `--repro`: a trace session started by hand aborts the server on the next segment (the engine bug, docs/engine-bugs) |
| 27 | test | `test replconfchain` | on a `goafts up` stand, HQbird 2.5/3.0: T1 node.json from the cluster and replconf activated by the node through fbagent, T3 the cluster's date, T5 a closed Firebird root opened again by fbagent, T4 (`--old-fbagent`) an agent without `replconf_install`, T2 (`--to-channel`) node and RCM updated by the agents |
| 24 | test | `test restartwait` | node 2027.4.4: a reinit to a replica whose restart window is closed shows the step `waiting`; the operator's restart ends the wait, and no second restart follows when the window opens |
| 20 | test | `test replconfdate` | HQbird 2.5/3.0: the valid date of the node's replconf file. The node's 30-day default, activation and auto-activation of a broken engine, a date written as fbagent writes it, expiry with the clock moved, the plugin before 2.1.0 on a DataGuard file (V-12) |
| – | `10-local` | `install --source local` (`hosts.<replica>.companion`) | a companion: a master-role node next to a replica node on the same Firebird, for `test promote` |
| – | `06-instance` | `hosts prepare` (Windows, `firebird.copy_of`) | a second Firebird instance on a Windows host: a copy of a root with its own port and service |
| – | `90-hostctl` | used by tb.py and tests | node API calls, service control, row counts, limbo check, firewall block, replica control files, state journal, replication.log and peer segment injection |

Details: [docs/modules.md](docs/modules.md). Tests: [docs/tests.md](docs/tests.md).
Adding a test: [docs/adding-tests.md](docs/adding-tests.md).
Russian quick guide: [docs/README.ru.md](docs/README.ru.md).

## Load options

```bash
python tb.py load start --db all --mode write --tx off              # same transaction kind all the time
python tb.py load start --db db1 --mode mixed --tx emul-safe        # changing isolation / wait / completion
python tb.py load start --db db1,db2 --mode read --tx full --conns 2:8 --minutes 30
python tb.py load status
python tb.py load stop
```

`--limbo` allows prepare-then-die transactions. It is off by default: the
CLI fb-loadgen does not resolve limbo on EMPLOYEE databases, and limbo crashed
Firebird 5.0.5 on Windows.

## Results

Every test writes `state/results/<test>-<time>/results.json` and `summary.md`,
and exits with 0 only when all cases passed.
