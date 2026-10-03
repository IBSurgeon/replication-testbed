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

**Companion** (Linux replica hosts, `hosts.<replica>.companion {node_id,
node_port}`): component `companion` installs a second node, role master, in
`<paths.node>/companion` with its own port and certificate (`gencerts` gets
its id), on the replica's Firebird and fbagent; its unit is
`hqclusternode-master-p<fb port>`. Its `databases.root` is empty until a
promote enrolls a database. Peers: the companion's are the other replicas;
each other replica lists the companion as a master. `rcm.json` gives the
replica and its companion one `host` label, which is how RCM pairs them for
"Promote to master". Removing the node removes the companion too; the DO
firewall opens its port between the droplets. `install --source goafts`
skips it.

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
| `cluster-prep` | for a goafts cluster (`tb.py goafts up`): the base folders `/opt/hqclusternode` and `/opt/hqbirdrcm` owned by firebird (the agent cannot create them; the fbagent installer's `--cluster` does the same), the databases root (firebird, 2770), the host's SYSDBA password in `agent_config.json` and in a node.json the agent already wrote |
| `channel --product-channel CH --self-update on\|off` | the update channel of hqclusternode and hqbirdrcm in `agent_config.json`, and the agent's own updates (`goafts.auto_update.enabled`); restarts the agent |
| `agent-swap --binary FILE` / `--restore true` | runs another fbagent build in place of the installed one (the old agent of `replconfchain` T4); the installed binary waits as `fbagent.tb-saved` |

CSR approval: with `goafts.admin` set in the local config (admin URL, client
certificate, key, admin token - or `p5ctl`, below) tb.py approves the CSRs of
the test bed hosts itself. Without it, approve them in the goafts admin panel
while tb.py waits. A pending CSR has no agent id yet, so tb.py approves a
request only when all of these hold: the host name in it is exactly the
host's name, or the host's name and six digits (fbagent 2.5x names the
request after its agent id, the host name and the enrollment date YYMMDD; no
other prefix match), it came from one of the host's addresses, it was made after this
enrollment began, and it is the only such request for that host. Anything
else is logged and left for a person: on a shared goafts it never approves
another bed's request.

goafts admin through fbagent's p5ctl: `goafts.admin.p5ctl` (the path of a
built `cmd/p5ctl-scratch`), `p5ctl_cwd` (the fbagent checkout whose
`ops/secrets/instances.yaml` holds the admin credentials) and `instance`
(e.g. `chess1`). The test bed config then holds nothing secret for goafts.

## Ports

`firebird.port` and `fbagent.port` may be left out (or 0). Then `install`
takes the Firebird port from `RemoteServicePort` in `firebird.conf` (3050 when
it is not set) and, for an `existing` agent, the fbagent port from its
`local_api.listen`, or, when it is not set, what the agent binds then: 10000
+ the Firebird port (13050 for 3050; fbagent `localapi.EffectiveListen`). A
new agent the test bed installs gets 13055. A port set in the config must
match what the host says, or `install` stops with both values.

## SYSDBA password of a host

`secrets.firebird_password` is SYSDBA's password on every host. A host whose
Firebird already has its own (a shared lab, an existing install) sets
`hosts.<name>.firebird.password` in the local config instead; the node on
that host and the modules use it. Never in a committed file.

## Engine of a host

`firebird.engine` ("2.5", "3.0", "4", "5"; empty: the node finds it) goes
into `node.json`. For 2.5/3.0 `replication_conf` is left out, so the node
uses its default replconf file. On Linux `hosts prepare` takes the installer
from `firebird_installer.linux_urls[<engine>]`, else `linux_url`. The
fbagent the test bed installs restarts only this instance:
`firebird.update.services` names its service (without it fbagent acts on
every HQbird instance of the host).

## 06-instance — a second instance on a Windows host (Windows)

For a pair on one Windows host (a copy next to the installed root, like
`Firebird30` + `Firebird30R`). A host with `firebird.copy_of` set gets it in
`hosts prepare`: the root is copied (no logs), the copy gets its own
`RemoteServicePort` (`firebird.port`), `IpcName`, `RemotePipeName` (2.5 also
`RemoteAuxPort = 0`) and the service `firebird.service` (manual start). An
HQbird 2.5/3.0 copy reads a copy of the source's replconf file, never the
source's own. `hosts wipe` removes only a copy the test bed made (marker
`.hqtb-instance`). The install also opens inbound rules for the node port
and RCM 7443 (`hqtb-*`, removed by uninstall) and deletes a `hqbirdrcm`
service of another folder, keeping the folder. Not run on a host yet.

## 30-dbs — test databases

`prepare` copies the source (default: Firebird's EMPLOYEE example) to
`<db-root>/<subdir>/db1..dbN/<file>` under an nbackup lock (`-L`, copy, `-N`)
and runs `-F` on every copy, so each copy is consistent and has its own GUID.
`remove` deletes `<db-root>/<subdir>`. On a replica the copies live in
`<replica db-root>/<master node id>/<subdir>`.

tb.py then does the replication part through the node API: `POST /v1/scansync`,
`POST /v1/firebird/restart`, `POST /v1/publication/sync`, and a standard
reinit of every database to every replica. On HQbird 2.5/3.0 it first
activates the replconf plugin on every node (`POST /v1/replconf/activate`)
and skips the publication step (these engines have none). `dbs remove` stops the load,
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

For the state machine tests: `statelog --db-id ID [--from LINE]` (state
changes of one database from the node's `journal.jsonl`; `--from -1` gives
the line count only), `replog-inject` (Firebird-format ERROR blocks appended
to replication.log), `peer-push --addr HOST:PORT` (a segment to another
node's `POST /v1/peer/segments`, with this node's certificate), `node-on-file`
(stop or kill the node when a `.delta` lock file appears or goes away),
`nbackup-unlock`, `db-new-guid` (the master file replaced by an nbackup copy
with a new GUID), `rcm-api` (RCM operator API on 127.0.0.1:7444, Digest login
from the secrets), `rcm-web --path P` (an RCM web page part such as
`/partials/db-table?tab=m3`: form login, session cookie, `HX-Request: true`),
`write-probe --db F` (one committed row in `TB_PROBE`; a replica refuses it), `db-header --db F` (`gstat -h`: Database GUID, Replication master GUID of HQbird 2.5/3.0, replication sequence, attributes), `db-copy-locked --db F --to F2 [--fixup seq|noseq|none] [--replica R]` (a copy as reinit makes one), `guid-promote --db F [--mode shutdown|stop] [--legacy true]` (a GUID of its own in place: replica mode off, single shutdown or Firebird stopped, `nbackup -L`, `-F` without `-SEQUENCE`, delta removed, validated, online), `segment-guids --glob 'P[;P]'` (GUID and number of journal segments), `replace-db --db F --with F2` (Firebird stopped, file replaced, started; Linux only), `attach --db F` (one attach through the server: HQbird 2.5/3.0 ask the replconf plugin on every attach), `clock --shift-days N` / `clock --epoch E` (the host clock N days on with time sync off; back to E with time sync on; Linux only), `trace --op list|start|stop|pid [--name N]` (the server's user trace sessions through `fbtracemgr`, one started in the background with a minimal config, and the server's PID; Linux only). For `legacy` (Linux): `hold-tx` (a writing transaction
left open N seconds, then rolled back), `node-conf-set` (one key of
node.json), `file-put` / `file-restore` / `file-copy`. `node-api --addr HOST:PORT` calls another node's API with
this node's certificate.
