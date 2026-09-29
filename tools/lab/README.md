# Local lab: HQbird 2.5 and 3.0 as applications

Private copies of HQbird 2.5 and 3.0 on the operator's Windows machine, for
the checks that need a real engine but no cluster (hqcluster-node
`docs/hqbird-25-30-finish-plan.md`, T-0 and track V). No node, no fbagent, no
network: one copy serves a master and a replica database at once.

## Safety

- The HQbird services the copies come from, and DataGuard, are **never**
  touched. `lab.py` starts and stops only processes whose executable is inside
  the lab directory.
- `create` refuses a lab port that is the port of an HQbird service or is
  taken by anything else.
- Every copy has its own `RemoteServicePort`, `IpcName`, `RemotePipeName`,
  lock directory (`FIREBIRD_LOCK`) and `replconf.properties`.
- A lab replconf file never gets a past date: HQbird then refuses every
  attach to the copy.

## Settings

The `lab` section of `config/testbed.local.json` (placeholders in
`config/testbed.example.json`):

| Key | What |
|---|---|
| `dir` | lab directory (default `state/lab`, git-ignored) |
| `hqbird30_root`, `hqbird25_root` | HQbird roots to copy |
| `port30`, `port25` | ports of the copies (default 3163, 3162) |
| `password` | SYSDBA password of the copies (never printed) |
| `stock_password` | SYSDBA password of a stock Firebird kit as shipped (`password25`, check V-23 on 2.5) |
| `replconf_repo` | local clone of IBSurgeon/replconf (`test/gen_samples.py` writes the lab files) |

## Use

```bash
python tools/lab/lab.py create 30          # copy, own port/IpcName, replconf.properties -> lab file
python tools/lab/lab.py create 25          # also emb25: embedded 2.5 client tools
python tools/lab/lab.py password25 Firebird-2.5.9.27139-0_Win32.zip   # 2.5 login over the port (see below)
python tools/lab/lab.py start 30
python tools/lab/lab.py status
python tools/lab/lab.py stop 30
python tools/lab/lab.py remove 30          # databases in <dir>/db30 are kept
python tools/lab/checks.py list            # the V checks
python tools/lab/checks.py run V-4 30      # one check on one copy
python tools/lab/golden.py 30 OUT          # golden replication.log for the node's parser tests
```

Layout of `<dir>`: `fb30`, `fb25` (the copies), `emb25` (2.5 client tools,
embedded), `conf30`, `conf25` (lab replconf files), `db30`, `db25`
(databases: `M.FDB` master, `R.FDB` replica, their log, archive and incoming
directories), `lock30`, `lock25`.

In Python:

```python
import lab, replconf_lab as rl
rl.standard('30')                     # M.FDB master and R.FDB replica on one copy
lab.restart('30')                     # replica records apply only at server start
print(lab.isql('30', 'select 1 from rdb$database;', rl.db('30', 'M.FDB')))
print(lab.header('30', rl.db('30', 'R.FDB')))
print(lab.replog('30'))
```

2.5 client tools run embedded (`emb25`), so they need no password and no
running server; the 2.5 server (`fb_inet_server -a -p <port>`) is still
needed for replication. The node logs in over the port, so the 2.5 copy needs
a known SYSDBA password: `password25` puts the stock `security2.fdb` of a
Firebird 2.5 zip kit into the copy (the HQbird one is kept as
`security2.fdb.hqbird`) and sets `lab.password` (2.5 uses its first 8
bytes).

## Findings

What the lab showed is recorded in hqcluster-node
`docs/hqbird-25-30-plan.md` (phase 1) and `docs/hqbird-25-30-finish-plan.md`
(track V).
