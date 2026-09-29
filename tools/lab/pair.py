"""A master and its replica on one lab copy, built from scratch in a
directory of their own: create the master, copy it under nbackup -L, fix up
the copy (-F: a new database GUID), mark it a replica of the master
(gfix -replica {master GUID as gstat prints it}), write both records and
restart the copy (replica records apply only at server start). ship() does
the node's part: it copies archived segments into the replica's incoming
directory."""
import os
import re
import shutil
import time

import lab
import replconf_lab as rl

NL = chr(10)


def tool(v, name):
    """A client tool of the copy, over the lab port: the server writes, so
    the server's plugin and replconf.properties apply. The 2.5 copy needs
    `lab.py password25` first."""
    return lambda args, input=None: lab.run(v, [lab.tool(v, name)] + args, input=input)


def target(v, path):
    return lab.dsn(v, path)


def isql(v, sql, path=''):
    args = [lab.tool(v, 'isql'), '-q'] + ([target(v, path)] if path else [])
    return lab.run(v, args, input=sql.encode('utf-8'))[1]


DDL = 'create table T (id integer not null primary key, s varchar(100));'


def create_db(v, path, ddl=DDL):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    sql = ("create database '%s' user 'SYSDBA' password '%s' page_size 8192 default character set utf8;" % (target(v, path), lab.PW)
           + NL + ddl + NL + 'commit;' + NL)
    out = isql(v, sql)
    if not os.path.exists(path):
        raise RuntimeError('create %s: %s' % (path, lab.mask(out)))
    return out


def db_guid(v, path):
    out = lab.header(v, path, keys=('Database GUID',))
    m = re.search(r'Database GUID:\s*(\{[0-9A-Fa-f-]+\})', out)
    if not m:
        raise RuntimeError('no GUID in gstat -h: %s' % out)
    return m.group(1)


def master_guid(v, path):
    out = lab.header(v, path, keys=('master GUID',))
    m = re.search(r'master GUID:\s*(\{[0-9A-Fa-f-]+\})', out)
    return m.group(1) if m else ''


USER = lambda: ['-user', 'SYSDBA', '-password', lab.PW]


def make_replica(v, master, replica, guid=None):
    """Copy of the master under nbackup -L, fixed up, marked a replica of
    guid (default: the master's own)."""
    if os.path.exists(replica):
        os.remove(replica)
    rc, out = tool(v, 'nbackup')(['-L', target(v, master)] + USER())
    if rc != 0:
        raise RuntimeError('nbackup -L: ' + lab.mask(out))
    try:
        shutil.copyfile(master, replica)
    finally:
        rc, out = tool(v, 'nbackup')(['-N', target(v, master)] + USER())
        if rc != 0:
            raise RuntimeError('nbackup -N: ' + lab.mask(out))
    rc, out = tool(v, 'nbackup')(['-F', replica])
    if rc != 0:
        raise RuntimeError('nbackup -F: ' + out)
    g = guid or db_guid(v, master)
    rc, out = tool(v, 'gfix')(['-replica', g, target(v, replica)] + USER())
    if rc != 0:
        raise RuntimeError('gfix -replica: ' + lab.mask(out))
    return g


def records(d, master, replica, timeout='5', master_other='', replica_other='', dirs=None):
    dirs = dirs or {}
    logdir = dirs.get('log', os.path.join(d, 'M.ReplLog'))
    arch = dirs.get('arch', os.path.join(d, 'M.LogArch'))
    inc = dirs.get('incoming', os.path.join(d, 'R.Incoming'))
    for sub in (logdir, arch, inc):
        os.makedirs(sub, exist_ok=True)
    return [rl.rec('hqcluster:lab:m', master, 'master', logdir, arch, timeout, other=master_other),
            rl.rec('hqcluster:lab:r', replica, 'replica', '', inc, other=replica_other)]


def build(v, d, master_name='M.FDB', replica_name='R.FDB', timeout='5', master_other='', replica_other='', ddl=DDL):
    """Creates the pair in d, writes the lab file with only this pair and
    restarts the copy. Returns (master, replica)."""
    lab.start(v)
    master, replica = os.path.join(d, master_name), os.path.join(d, replica_name)
    for sub in ('M.ReplLog', 'M.LogArch', 'R.Incoming'):
        shutil.rmtree(os.path.join(d, sub), ignore_errors=True)
    rl.write(v, [])
    lab.restart(v)
    create_db(v, master, ddl)
    make_replica(v, master, replica)
    rl.write(v, records(d, master, replica, timeout, master_other, replica_other))
    lab.restart(v)
    return master, replica


def count(v, path, table='T'):
    out = isql(v, 'select count(*) from %s;' % table, path)
    m = re.findall(r'^\s*(\d+)\s*$', out, re.M)
    return int(m[-1]) if m else None


def segments(d, arch=None):
    arch = arch or os.path.join(d, 'M.LogArch')
    return sorted(f for f in os.listdir(arch) if '.arch-' in f and not f.endswith('.shipped')) if os.path.isdir(arch) else []


def ship(d, arch=None, inc=None):
    """Copies the master's archived segments into the replica's incoming
    directory, as the node does. Returns the new ones."""
    arch = arch or os.path.join(d, 'M.LogArch')
    inc = inc or os.path.join(d, 'R.Incoming')
    new = []
    for f in segments(d, arch):
        if os.path.exists(os.path.join(arch, f + '.shipped')):
            continue
        shutil.copyfile(os.path.join(arch, f), os.path.join(inc, f + '.tmp'))
        os.replace(os.path.join(inc, f + '.tmp'), os.path.join(inc, f))
        open(os.path.join(arch, f + '.shipped'), 'w').close()
        new.append(f)
    return new


def wait_replicated(v, d, replica, want, table='T', secs=60, arch=None, inc=None):
    """Ships segments until the replica has want rows; the last count."""
    n = None
    for _ in range(secs):
        ship(d, arch, inc)
        n = count(v, replica, table)
        if n == want:
            return n
        time.sleep(1)
    return n
