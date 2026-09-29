#!/usr/bin/env python3
"""The V checks of hqcluster-node docs/hqbird-25-30-finish-plan.md that run
on the local lab copies. Each builds its own master/replica pair in
<lab dir>/v<version>/<check> and prints what it saw.

    python tools/lab/checks.py list
    python tools/lab/checks.py run V-4 30|25
"""
import os
import re
import shutil
import sys
import time
import uuid

import lab
import pair
import replconf_lab as rl

NL = chr(10)
Q = chr(39)


def say(*a):
    print(*a, flush=True)


def sql_str(s):
    return Q + s.replace(Q, Q + Q) + Q


def insert(v, path, first, n=1, table='T', text='x'):
    rows = NL.join('insert into %s values (%d, %s);' % (table, i, sql_str(text)) for i in range(first, first + n))
    return pair.isql(v, rows + NL + 'commit;' + NL, path)


def fresh(v, name):
    d = os.path.join(lab.DIR, 'v' + v, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


# V-4: a non-ASCII path in the records. Values in a replconf file are hex of
# bytes; the node writes UTF-8. Is it UTF-8 or the ANSI code page that the
# engine matches against the database it opens?
def v4(v):
    import gen_samples
    ansi = 'cp1252'
    d = fresh(v, 'v4_Donn' + chr(0xE9) + 'es')
    master, replica = os.path.join(d, 'M.FDB'), os.path.join(d, 'R.FDB')
    lab.start(v)
    rl.write(v, [])
    lab.restart(v)
    # isql reads the script in the ANSI code page: the path must be sent so.
    def isql_ansi(sql, path=''):
        args = [lab.tool(v, 'isql'), '-q'] + ([pair.target(v, path)] if path else [])
        return lab.run(v, args, input=sql.encode(ansi))[1]
    out = isql_ansi("create database '%s' user 'SYSDBA' password '%s' page_size 8192;" % (pair.target(v, master), lab.PW)
                    + NL + pair.DDL + NL + 'commit;' + NL)
    say('create database in a non-ASCII directory:', 'file exists' if os.path.exists(master) else 'NO FILE ' + lab.mask(out)[-300:])
    if not os.path.exists(master):
        return
    # gstat/nbackup/gfix get the path as an argument: Windows passes it in the ANSI code page.
    pair.make_replica(v, master, replica)
    say('replica made:', pair.master_guid(v, replica))
    results = {}
    for enc in ('utf-8', ansi):
        orig = gen_samples.hexs
        gen_samples.hexs = lambda s, e=enc: s.encode(e).hex().upper()
        try:
            for sub in ('M.ReplLog', 'M.LogArch', 'R.Incoming'):
                shutil.rmtree(os.path.join(d, sub), ignore_errors=True)
            rl.write(v, pair.records(d, master, replica))
        finally:
            gen_samples.hexs = orig
        lab.restart(v)
        base = pair.count(v, replica) if False else None
        out = isql_ansi('insert into T values (%d, %s);' % (100 if enc == 'utf-8' else 200, sql_str(enc)) + NL + 'commit;' + NL, master)
        failed = 'Statement failed' in out or 'error' in out.lower()
        time.sleep(8)
        segs = pair.segments(d)
        shipped = pair.ship(d)
        time.sleep(12)
        rows = isql_ansi('select count(*) from T;', replica)
        n = re.findall(r'^\s*(\d+)\s*$', rows, re.M)
        results[enc] = (failed, segs, n[-1] if n else rows[-200:])
        say('record paths in %s: master write %s; segments %s; replica rows %s' % (
            enc, 'FAILED ' + lab.mask(out)[-300:] if failed else 'ok', segs, n[-1] if n else rows[-200:]))
    say(lab.replog(v, 10))
    return results


# V-10: RDB$PING_REPLICATION() on the master and the replica.
def v10(v):
    d = fresh(v, 'v10')
    master, replica = pair.build(v, d)
    for name, path in (('master', master), ('replica', replica)):
        for sql in ('select rdb$ping_replication() from rdb$database;',
                    "select rdb$get_context('SYSTEM', 'REPLICA_MODE') from rdb$database;",
                    "select rdb$get_context('SYSTEM', 'REPLICATION_SEQUENCE') from rdb$database;"):
            out = pair.isql(v, sql, path)
            lines = [l.strip() for l in out.splitlines() if l.strip() and not l.startswith('Database') and 'SQL>' not in l]
            say('%-7s %-70s -> %s' % (name, sql, ' | '.join(lines)[:200]))


# V-20: the replica's log when its header names another master GUID.
def v20(v):
    d = fresh(v, 'v20')
    master, replica = pair.build(v, d)
    insert(v, master, 1)
    say('baseline replicated rows:', pair.wait_replicated(v, d, replica, 1))
    lab.stop(v)
    foreign = '{' + str(uuid.uuid4()).upper() + '}'
    lab.start(v)
    rc, out = pair.tool(v, 'gfix')(['-replica', foreign, pair.target(v, replica)] + pair.USER())
    say('gfix -replica foreign:', rc, lab.mask(out).strip()[:200], '->', pair.master_guid(v, replica))
    lab.restart(v)
    insert(v, master, 2)
    time.sleep(8)
    pair.ship(d)
    time.sleep(25)
    log = lab.replog(v, 30)
    say(log)
    say('replica rows now:', pair.count(v, replica))
    return log


# V-22: which side reads master_priority, compress_records,
# exclude_without_pk and alert_command.
NP_DDL = pair.DDL + NL + 'create table NP (id integer, s varchar(1000));' + NL + 'create table C (id integer not null primary key, s varchar(1000));'


def v22(v, only=''):
    out = {}
    # exclude_without_pk: rows of a table without a primary key.
    for where in (('none', 'master', 'replica') if only in ('', 'expk') else ()):
        d = fresh(v, 'v22_expk_' + where)
        m_o = 'exclude_without_pk=true' if where == 'master' else ''
        r_o = 'exclude_without_pk=true' if where == 'replica' else ''
        master, replica = pair.build(v, d, master_other=m_o, replica_other=r_o, ddl=NP_DDL)
        insert(v, master, 1, 3, table='NP')
        insert(v, master, 1, 1)
        pair.wait_replicated(v, d, replica, 1)
        time.sleep(5)
        pair.ship(d)
        time.sleep(12)
        out['exclude_without_pk@' + where] = (pair.count(v, replica, 'NP'), pair.count(v, replica))
        say('exclude_without_pk on %-7s: replica NP rows %s, T rows %s' % ((where,) + out['exclude_without_pk@' + where]))
    # compress_records: size of the segment of the same load.
    for where in (('none', 'master', 'replica') if only in ('', 'compress') else ()):
        d = fresh(v, 'v22_comp_' + where)
        m_o = 'compress_records=true' if where == 'master' else ''
        r_o = 'compress_records=true' if where == 'replica' else ''
        master, replica = pair.build(v, d, master_other=m_o, replica_other=r_o, ddl=NP_DDL)
        insert(v, master, 1, 200, table='C', text='A' * 900)
        time.sleep(10)
        segs = pair.segments(d)
        size = sum(os.path.getsize(os.path.join(d, 'M.LogArch', s)) for s in segs)
        n = pair.wait_replicated(v, d, replica, 200, table='C')
        out['compress_records@' + where] = (size, n)
        say('compress_records on %-7s: segment bytes %d, replica rows %s' % (where, size, n))
    # master_priority: the replica already has the row the master inserts.
    for where in (('none', 'master', 'replica') if only in ('', 'priority') else ()):
        d = fresh(v, 'v22_prio_' + where)
        m_o = 'master_priority=true' if where == 'master' else ''
        r_o = 'master_priority=true' if where == 'replica' else ''
        flag = os.path.join(d, 'alert.txt')
        alert = 'alert_command=cmd /c echo %%s > "%s"' % flag
        master, replica = pair.build(v, d, master_other=(m_o + NL + alert).strip(), replica_other=(r_o + NL + alert).strip())
        # Make the replica writable for a moment, put the conflicting row, back to replica.
        g = pair.db_guid(v, master)
        pair.tool(v, 'gfix')(['-replica', '{}', pair.target(v, replica)] + pair.USER())
        pair.isql(v, 'insert into T values (7, ' + sql_str('replica') + ');' + NL + 'commit;' + NL, replica)
        pair.tool(v, 'gfix')(['-replica', g, pair.target(v, replica)] + pair.USER())
        lab.restart(v)
        pair.isql(v, 'insert into T values (7, ' + sql_str('master') + ');' + NL + 'insert into T values (8, ' + sql_str('master') + ');' + NL + 'commit;' + NL, master)
        time.sleep(8)
        pair.ship(d)
        time.sleep(20)
        rows = pair.isql(v, 'select id, s from T order by id;', replica)
        vals = re.findall(r'^\s*(\d+)\s+(\w+)\s*$', rows, re.M)
        log = lab.replog(v, 60)
        err = [l.strip() for l in log.splitlines() if 'ERROR' in l or 'violation' in l.lower() or 'duplicate' in l.lower()]
        out['master_priority@' + where] = (vals, err[-2:], os.path.exists(flag))
        say('master_priority on %-7s: replica rows %s; errors %s; alert_command ran: %s' % (where, vals, err[-2:], os.path.exists(flag)))
    return out


# V-23: a database made by stock Firebird (no HQbird): does it have a GUID,
# and does HQbird give it one on the first attach?
def v23(v, kit):
    d = fresh(v, 'v23')
    path = os.path.join(d, 'VANILLA.FDB')
    import subprocess
    server = None
    if v == '25':
        # A stock 2.5 zip kit (no embedded engine in it): its SuperServer
        # runs as an application on a port of its own (SYSDBA with
        # lab.stock_password).
        import zipfile
        root = os.path.join(lab.DIR, 'stock25')
        if not os.path.exists(root):
            zipfile.ZipFile(kit).extractall(root)
            lab.set_conf(os.path.join(root, 'firebird.conf'), {'RemoteServicePort': '3172', 'IpcName': 'FB_STOCK25',
                                                               'RemotePipeName': 'fb_stock25', 'RemoteAuxPort': '0'})
        bindir = os.path.join(root, 'bin')
        e = dict(os.environ, FIREBIRD=root, ISC_USER='SYSDBA', ISC_PASSWORD=lab.STOCK_PW, FIREBIRD_LOCK=os.path.join(lab.DIR, 'lockstock25'))
        os.makedirs(e['FIREBIRD_LOCK'], exist_ok=True)
        server = subprocess.Popen([os.path.join(bindir, 'fbserver.exe'), '-a', '-p', '3172'], cwd=bindir, env=e,
                                  creationflags=0x00000008, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            if lab.listening(3172):
                break
            time.sleep(0.5)
        dsn = 'localhost/3172:' + path
    else:
        # A stock 3.0 kit, embedded (no server, no login), copied into the
        # lab with an IpcName of its own: the default one belongs to a
        # running service.
        root = os.path.join(lab.DIR, 'stock30')
        if not os.path.exists(root):
            shutil.copytree(kit, root, ignore=shutil.ignore_patterns('*.log', 'doc', 'examples', 'help'))
            lab.set_conf(os.path.join(root, 'firebird.conf'), {'IpcName': 'FB_STOCK30', 'RemotePipeName': 'fb_stock30',
                                                               'RemoteServicePort': '3173'})
        bindir = root
        e = dict(os.environ, FIREBIRD=root, ISC_USER='SYSDBA', ISC_PASSWORD='x', FIREBIRD_LOCK=os.path.join(lab.DIR, 'lockstock30'))
        dsn = path
    os.makedirs(e['FIREBIRD_LOCK'], exist_ok=True)
    # Shared memory names do not follow IpcName: no lab copy of the same
    # version may run meanwhile.
    lab.stop(v)
    p = subprocess.run([os.path.join(bindir, 'isql.exe'), '-q'], input=("create database '%s';" % dsn + NL + pair.DDL + NL + 'commit;' + NL).encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e, cwd=bindir)
    say('stock create:', p.returncode, p.stdout.decode('utf-8', 'replace')[-200:].strip())
    p = subprocess.run([os.path.join(bindir, 'gstat.exe'), '-h', dsn], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e, cwd=bindir)
    stock = [l.strip() for l in p.stdout.decode('utf-8', 'replace').splitlines() if 'GUID' in l or 'ODS' in l]
    if server:
        server.kill()
        server.wait()
    say('stock gstat -h:', stock)
    say('HQbird gstat -h before any attach:', lab.header(v, path, keys=('GUID', 'ODS')).replace(NL, ' | '))
    lab.start(v)
    say('HQbird attach:', pair.isql(v, 'select count(*) from T;', path).strip().replace(NL, ' ')[-120:])
    say('HQbird gstat -h after an attach:', lab.header(v, path, keys=('GUID', 'ODS')).replace(NL, ' | '))


CHECKS = {'V-4': v4, 'V-10': v10, 'V-20': v20, 'V-22': v22, 'V-23': v23}


def main(argv):
    if len(argv) >= 2 and argv[1] == 'list':
        for k, f in CHECKS.items():
            print(k, (f.__doc__ or '').strip())
        return
    if len(argv) < 4 or argv[1] != 'run' or argv[2] not in CHECKS or argv[3] not in lab.VERSIONS:
        sys.exit(__doc__)
    CHECKS[argv[2]](argv[3], *argv[4:])


if __name__ == '__main__':
    main(sys.argv)
