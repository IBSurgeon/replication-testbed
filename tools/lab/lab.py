#!/usr/bin/env python3
"""Local lab: private copies of HQbird 2.5 and 3.0 run as applications on the
operator's Windows machine, for the checks that need a real engine but no
cluster (hqcluster-node docs/hqbird-25-30-finish-plan.md, T-0 and track V).

A copy has its own port, IpcName and RemotePipeName, its own lock directory
and its own replconf.properties pointing at a lab replconf file. The HQbird
services the copies come from, and DataGuard, are never touched: the lab
only starts and stops processes whose executable is inside the lab directory,
and `create` refuses a port that is already taken.

Settings come from the "lab" section of config/testbed.local.json (see
config/testbed.example.json); the SYSDBA password of the copies is
lab.password there and is never printed.

    python tools/lab/lab.py create 30|25     copy the HQbird root into the lab
    python tools/lab/lab.py start|stop|restart 30|25
    python tools/lab/lab.py status
    python tools/lab/lab.py isql 30|25 DB    SQL from stdin
    python tools/lab/lab.py remove 30|25     stop and delete the copy
    python tools/lab/lab.py password25 ZIP   2.5 copy: stock security2.fdb, SYSDBA -> lab.password
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
LOCAL = os.path.join(REPO, 'config', 'testbed.local.json')

VERSIONS = ('30', '25')
# Server executable and the directory of the tools, relative to the root.
SERVER_EXE = {'30': ('firebird.exe', ''), '25': ('fb_inet_server.exe', 'bin')}
DEFAULT_PORT = {'30': 3163, '25': 3162}


def cfg():
    try:
        c = json.load(open(LOCAL, encoding='utf-8'))
    except OSError:
        sys.exit('no %s: copy config/testbed.example.json and fill in the "lab" section' % LOCAL)
    lab = c.get('lab') or {}
    if not lab.get('password'):
        sys.exit('lab.password is not set in config/testbed.local.json')
    return lab


LAB = cfg()
DIR = os.path.abspath(LAB.get('dir') or os.path.join(REPO, 'state', 'lab'))
PW = LAB['password']
# SYSDBA password of a stock Firebird kit (its security database as shipped).
STOCK_PW = LAB.get('stock_password', '')


def root(v):
    return os.path.join(DIR, 'fb' + v)


def port(v):
    return int(LAB.get('port' + v) or DEFAULT_PORT[v])


def source(v):
    return LAB.get('hqbird%s_root' % v) or (r'C:\HQbird\Firebird30' if v == '30' else r'C:\HQbird\Firebird25')


def conf_path(v):
    """The lab replconf file the copy's replconf.properties points to."""
    return os.path.join(DIR, 'conf' + v, 'replconf.lab.hqbird')


def props_path(v):
    sub = SERVER_EXE[v][1]
    return os.path.join(root(v), sub, 'replconf.properties')


def env(v):
    return dict(os.environ, ISC_USER='SYSDBA', ISC_PASSWORD=PW, FIREBIRD=root(v),
                FIREBIRD_LOCK=os.path.join(DIR, 'lock' + v))


def tool(v, name):
    return os.path.join(root(v), SERVER_EXE[v][1], name + '.exe')


def dsn(v, path):
    return 'localhost/%d:%s' % (port(v), path)


def mask(text):
    return text.replace(PW, '***') if PW else text


def run(v, args, input=None, timeout=120):
    p = subprocess.run(args, input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env(v), timeout=timeout)
    return p.returncode, mask(p.stdout.decode('utf-8', 'replace'))


def isql(v, sql, db=''):
    """isql of the copy over its own port (3.0), or embedded (2.5, see emb25)."""
    if v == '25':
        return emb25(['isql', '-q'] + ([db] if db else []), input=sql.encode())[1]
    args = [tool(v, 'isql'), '-q'] + ([dsn(v, db)] if db else [])
    return run(v, args, input=sql.encode())[1]


def header(v, path, keys=('ODS', 'GUID', 'Attributes', 'eplica', 'Generation', 'Sequence')):
    if v == '25':
        rc, out = emb25(['gstat', '-h', path])
    else:
        rc, out = run(v, [tool(v, 'gstat'), '-h', dsn(v, path)])
    return '\n'.join(l for l in out.splitlines() if any(k in l for k in keys)) or out


def emb25(args, input=None, timeout=120):
    """2.5 client tools run embedded (fbembed.dll as fbclient.dll in emb25):
    the copy's security database is not needed."""
    e = dict(os.environ, FIREBIRD=root('25'), FIREBIRD_LOCK=os.path.join(DIR, 'lock25'), ISC_USER='SYSDBA', ISC_PASSWORD='x')
    exe = os.path.join(DIR, 'emb25', args[0] + '.exe')
    p = subprocess.run([exe] + args[1:], input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e,
                       cwd=os.path.join(DIR, 'emb25'), timeout=timeout)
    return p.returncode, p.stdout.decode('utf-8', 'replace')


def listening(p):
    try:
        socket.create_connection(('127.0.0.1', p), 1).close()
        return True
    except OSError:
        return False


def lab_processes(v=None):
    base = root(v) if v else DIR
    ps = ("Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%s\\*' } | "
          "ForEach-Object { '{0} {1}' -f $_.Id, $_.Path }") % base
    out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], stdout=subprocess.PIPE).stdout.decode('utf-8', 'replace')
    return [l for l in out.splitlines() if l.strip()]


def start(v):
    if listening(port(v)):
        return True
    exe, sub = SERVER_EXE[v]
    DETACHED = 0x00000008
    args = [os.path.join(root(v), sub, exe), '-a'] + (['-p', str(port(v))] if v == '25' else [])
    subprocess.Popen(args, cwd=os.path.join(root(v), sub), env=env(v), creationflags=DETACHED,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if listening(port(v)):
            return True
        time.sleep(0.5)
    return False


def stop(v):
    """Stops only processes whose executable is inside this copy."""
    r = root(v)
    ps = "Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%s\\*' } | ForEach-Object { Stop-Process -Id $_.Id -Force }" % r
    subprocess.run(['powershell', '-NoProfile', '-Command', ps])
    time.sleep(1)


def restart(v):
    stop(v)
    return start(v)


def replog(v, n=40):
    p = os.path.join(root(v), 'replication.log')
    if not os.path.exists(p):
        return 'no replication.log'
    return '\n'.join(open(p, errors='replace').read().splitlines()[-n:])


def set_conf(path, settings):
    """Appends or replaces top-level keys of a firebird.conf."""
    lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
    out, seen = [], set()
    for line in lines:
        key = line.split('=', 1)[0].strip()
        if '=' in line and not line.lstrip().startswith('#') and key in settings:
            out.append('%s = %s' % (key, settings[key]))
            seen.add(key)
        else:
            out.append(line)
    for k, val in settings.items():
        if k not in seen:
            out.append('%s = %s' % (k, val))
    open(path, 'w', encoding='utf-8', newline='\r\n').write('\n'.join(out) + '\n')


def service_ports():
    """Ports of the HQbird copies' sources: the lab never uses them."""
    out = set()
    for v in VERSIONS:
        conf = os.path.join(source(v), 'firebird.conf')
        if os.path.exists(conf):
            for line in open(conf, encoding='utf-8', errors='replace'):
                if line.strip().startswith('RemoteServicePort'):
                    out.add(int(line.split('=', 1)[1].strip()))
    return out


def create(v):
    r = root(v)
    if port(v) in service_ports():
        sys.exit('lab port %d is the port of an HQbird service: choose another lab.port%s' % (port(v), v))
    if listening(port(v)) and not lab_processes(v):
        sys.exit('port %d is taken by something that is not the lab' % port(v))
    if not os.path.exists(r):
        print('copy %s -> %s' % (source(v), r))
        shutil.copytree(source(v), r, ignore=shutil.ignore_patterns('*.log', '*.lck', 'replication.log*'))
    set_conf(os.path.join(r, 'firebird.conf'), {
        'RemoteServicePort': str(port(v)),
        'IpcName': 'FB_LAB' + v,
        'RemotePipeName': 'fb_lab' + v,
        **({'RemoteAuxPort': '0'} if v == '25' else {}),
    })
    os.makedirs(os.path.join(DIR, 'lock' + v), exist_ok=True)
    os.makedirs(os.path.dirname(conf_path(v)), exist_ok=True)
    os.makedirs(os.path.join(DIR, 'db' + v), exist_ok=True)
    open(props_path(v), 'w', newline='\r\n').write(conf_path(v) + '\n')
    if v == '25':
        emb = os.path.join(DIR, 'emb25')
        if not os.path.exists(emb):
            shutil.copytree(os.path.join(r, 'bin'), emb)
            shutil.copy(os.path.join(r, 'firebird.msg'), emb)
            shutil.copytree(os.path.join(r, 'intl'), os.path.join(emb, 'intl'))
            shutil.copy(os.path.join(emb, 'fbembed.dll'), os.path.join(emb, 'fbclient.dll'))
        # Embedded writes read this one.
        open(os.path.join(emb, 'replconf.properties'), 'w', newline='\r\n').write(conf_path(v) + '\n')
    print('lab %s: %s, port %d, replconf.properties -> %s' % (v, r, port(v), conf_path(v)))


def password25(zip_path):
    """Gives the 2.5 copy a known SYSDBA password (lab.password), so the
    node's isql can log in over the port: the copy's security2.fdb (kept as
    security2.fdb.hqbird) is replaced by the stock one of a Firebird 2.5 zip
    kit (SYSDBA with lab.stock_password), then gsec changes the password.
    2.5 uses only the first 8 bytes of a password."""
    import zipfile
    if not STOCK_PW:
        sys.exit('lab.stock_password is not set in config/testbed.local.json')
    sec = os.path.join(root('25'), 'security2.fdb')
    z = zipfile.ZipFile(zip_path)
    name = next(n for n in z.namelist() if n.lower().endswith('security2.fdb'))
    stop('25')
    if not os.path.exists(sec + '.hqbird'):
        shutil.copy(sec, sec + '.hqbird')
    open(sec, 'wb').write(z.read(name))
    if not start('25'):
        sys.exit('the 2.5 copy did not start')
    e = dict(env('25'), ISC_PASSWORD=STOCK_PW)
    p = subprocess.run([tool('25', 'gsec'), '-user', 'SYSDBA', '-password', STOCK_PW, '-database',
                        'localhost/%d:%s' % (port('25'), sec), '-modify', 'SYSDBA', '-pw', PW],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=e)
    print('gsec: exit %d %s' % (p.returncode, mask(p.stdout.decode('utf-8', 'replace')).strip()))


def remove(v):
    stop(v)
    shutil.rmtree(root(v), ignore_errors=True)
    if v == '25':
        shutil.rmtree(os.path.join(DIR, 'emb25'), ignore_errors=True)
    print('lab %s removed (databases in %s kept)' % (v, os.path.join(DIR, 'db' + v)))


def status():
    for v in VERSIONS:
        print('lab %s: %s, port %d %s, replconf.properties -> %s' % (
            v, root(v) if os.path.exists(root(v)) else '(not created)', port(v),
            'up' if listening(port(v)) else 'down',
            open(props_path(v)).read().strip() if os.path.exists(props_path(v)) else '-'))
    for line in lab_processes():
        print('  process', line)


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd, v = argv[1], (argv[2] if len(argv) > 2 else '')
    if cmd == 'status':
        return status()
    if cmd == 'password25':
        return password25(v)
    if v not in VERSIONS:
        sys.exit('version: 30 or 25')
    if cmd == 'create':
        create(v)
    elif cmd == 'remove':
        remove(v)
    elif cmd == 'start':
        print('up' if start(v) else 'did not start')
    elif cmd == 'stop':
        stop(v)
    elif cmd == 'restart':
        print('up' if restart(v) else 'did not start')
    elif cmd == 'isql':
        print(isql(v, sys.stdin.read(), argv[3] if len(argv) > 3 else ''))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main(sys.argv)
