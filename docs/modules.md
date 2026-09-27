# Modules

A module is one script per OS with the same name and the same arguments:
`modules/linux/NN-name.sh` and `modules/windows/NN-name.ps1`. It runs ON the
host, as root (Linux) or an administrator (Windows), and takes
`<command> --key value ...`. The last line `TBRESULT <json>` is its result
for tb.py. A module never prints a secret, except on a `TBSECRET key=value`
line, which tb.py hides from its log.

tb.py copies the modules to `<work>/modules` (Linux `/opt/hqtb`, Windows
`C:\hqtb`) and writes `<work>/secrets.env` (KEY=VALUE, readable by the admin
only). You can also run a module by hand on a host:

```bash
sudo bash /opt/hqtb/modules/30-dbs.sh list --db-root /databases/master --subdir tb
```

```powershell
powershell -ExecutionPolicy Bypass -File C:\hqtb\modules\30-dbs.ps1 list --db-root C:\hqtb\databases\master
```

## Stage folder

Install modules take their input from `<work>/stage`:

| Path | Content |
|---|---|
| `bin/` | `fbagent`, `hqclusternode`, `hqbirdrcm`, `fb-loadgen` (`.exe` on Windows) |
| `conf/node.json`, `conf/rcm.json` | rendered by tb.py from the local config |
| `certs/` | `ca.crt` and this node's `<node_id>.crt/.key` |
| `rcm-certs/` | `ca.crt`, `rcm.crt`, `rcm.key` |

The cluster CA is made once with `hqclusternode gencerts` on the master; the
CA key is kept only in `state/certs/` on the operator machine.

## 10-local — install from local copies

| Command | Does |
|---|---|
| `detect` | Firebird root, Firebird systemd unit (Linux), host name, IP addresses, `RemoteServicePort` from `firebird.conf`. Changes nothing |
| `install --components fbagent,node,rcm` | fbagent (`--fbagent-mode install`: new agent with `local_api` only; `existing`: check the agent HQbird installed), node service (`hqclusternode svc install`), RCM service (`hqbirdrcm svc install`) |
| `uninstall --components ...` | stops and removes the services and folders, stops the processes still running from them (fbtracemgr, hqmonitor, ...), removes fbagent's `/var/lib/hqmonitor/<id>`, its update backups and its trace sessions; restores the `replication.conf` saved before the first install and restarts Firebird. Then checks what is left and fails with the list |
| `wipe` | everything the test bed put on the host: load processes, `tb-block` firewall rules, rcm, node, an fbagent it installed (an `existing` agent stays); then the same check. `tb.py hosts wipe --hosts H --yes` also removes the work folder |
| `fbagent-info` | `local_api` settings of an existing agent (the token on a `TBSECRET` line) |

## 20-goafts — install from a goafts server

`--url` is the bootstrap URL (`https://<host>:9443`). `--pin` is the SPKI
SHA-256 of the goafts TLS certificate (64 hex), the value of
`GET <url>/v1/bootstrap/pin`. Downloads trust the server only by this pin and
check the sha256 of the release metadata.

| Command | Does |
|---|---|
| `download --products fbagent,hqclusternode,hqbirdrcm` | pinned download to `stage/bin` |
| `enroll` | `fbagent --setup <fb root> --bootstrap-url --server-pin`, waits until the CSR is approved; then turns on `local_api` and installs the agent service |
| `install --product-install direct` | registers the node / RCM services from the downloaded binaries |
| `install --product-install agent` | puts node.json/rcm.json and certs in place, sets `<product>.update.install_enabled`, runs `fbagent --product-update <id> --apply` (needs the products published on goafts) |
| `uninstall` | as in 10-local, with the same check of what is left; `tb.py uninstall --deregister` also deletes the agent on goafts through the admin API |

CSR approval: with `goafts.admin` set in the local config (admin URL, client
certificate, key, admin token) tb.py approves the CSRs of the test bed hosts
itself. Without it, approve them in the goafts admin panel while tb.py waits.
A pending CSR has no agent id yet, so tb.py approves a request only when all
of these hold: the host name in it is exactly the host's name (no prefix
match), it came from one of the host's addresses, it was made after this
enrollment began, and it is the only such request for that host. Anything
else is logged and left for a person: on a shared goafts it never approves
another bed's request.

## Ports

`firebird.port` and `fbagent.port` may be left out (or 0). Then `install`
takes the Firebird port from `RemoteServicePort` in `firebird.conf` (3050 when
it is not set) and, for an `existing` agent, the fbagent port from its
`local_api.listen` (13055, fbagent's own default, when it is not set). A new
agent gets 13055. A port set in the config must match what the host says, or
`install` stops with both values.

## 30-dbs — test databases

`prepare` copies the source (default: Firebird's EMPLOYEE example) to
`<db-root>/<subdir>/db1..dbN/<file>` under an nbackup lock (`-L`, copy, `-N`)
and runs `-F` on every copy, so each copy is consistent and has its own GUID.
`remove` deletes `<db-root>/<subdir>`. On a replica the copies live in
`<replica db-root>/<master node id>/<subdir>`.

tb.py then does the replication part through the node API: `POST /v1/scansync`,
`POST /v1/firebird/restart`, `POST /v1/publication/sync`, and a standard
reinit of every database to every replica. `dbs remove` stops the load,
removes the files on the master and the replicas, runs scansync with
`allow_shrink`, restarts Firebird, and forgets the ORPHANED records.

## 40-loadgen and 50-load

tb.py builds fb-loadgen with `go build` for the target OS (from the git URL
in the config, a local checkout, or takes a ready binary). `40-loadgen install`
puts it in `<work>/loadgen`; `smoke` runs it for 5 s and checks the final
report and `Total: N > 0`.

`50-load start` starts one fb-loadgen per database (two for `mixed`), detached
from the ssh session (Linux `setsid nohup`, Windows WMI `Win32_Process.Create`).
Logs and pid files: `<work>/load/<tag>/`.

| Option | Values |
|---|---|
| `--mode` | `write` (write-heavy), `read` (read-heavy), `mixed` (both), `spike`, `oltp-emul` (needs a `fb-loadgen provision` database) |
| `--tx` | `off`: no transaction changes; `emul-safe`: isolation, wait and completion change per transaction; `full`: also consistency isolation and infinite wait |
| `--limbo` | allow limbo transactions (off by default) |
| `--extended` | `true`: bulk and heavy operations too; `false`: classic run |
| `--conns` | `MIN:MAX` connections per process |
| `--minutes` | `0`: until `stop` |

fb-loadgen takes the password only as `--pass`, so it shows in the process
list of the master host.

## 90-hostctl — host actions

`node-api` (runs `hqclusternode api ... -i` with the installed node.json and
certs), `node-svc stop|start|restart|kill`, `fb-svc stop|start|restart|status`,
`counts` (row count of every user table; keyed tables must match),
`limbo` (`gfix -list`), `files`, `remove-file`, `block-peer` / `unblock-peer`
(iptables on Linux, Windows Firewall rules on Windows), `tail`,
`replctl --dir D` (Firebird's replica control files `{GUID}` in a journal
source folder: applied position, `db_sequence`, active transactions).
