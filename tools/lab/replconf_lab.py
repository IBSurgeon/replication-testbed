"""Lab replconf files: records in the shape the node writes, written with the
plugin repository's test generator (IBSurgeon/replconf test/gen_samples.py;
its path is lab.replconf_repo in the local config)."""
import os
import sys

import lab

sys.path.insert(0, os.path.join(lab.LAB.get('replconf_repo') or os.path.join(lab.REPO, '..', 'replconf'), 'test'))
import gen_samples  # noqa: E402


def rec(regid, path, role, logdir='', archdir='', timeout='', verbose='true', other='', auth=''):
    return [("DBRegID", regid), ("DatabaseFile", path), ("replica_database", ""), ("replication_auth", auth),
            ("replication_role", role), ("replication_mode", "async"), ("repparam_log_directory", logdir),
            ("repparam_log_archive_directory", archdir), ("repparam_log_archive_command", ""),
            ("repparam_verbose", verbose), ("repparam_log_archive_timeout", timeout), ("repparam_other_params", other)]


def write(v, records, valid='2099-12-31', fmt='v2.0'):
    """Writes the lab file the copy reads. Never an expired date: the plugin
    then fails every attach to the instance."""
    gen_samples.write(lab.conf_path(v), fmt, valid, [tuple(map(tuple, r)) for r in records], nl='\r\n')


def db(v, name=''):
    d = os.path.join(lab.DIR, 'db' + v)
    return os.path.join(d, name) if name else d


def standard(v, timeout='10', master_other='', replica_other='', auth=''):
    """A master M.FDB and a replica R.FDB on the same copy."""
    m, r = db(v, 'M.FDB'), db(v, 'R.FDB')
    for d in ('M.FDB.ReplLog', 'M.FDB.LogArch', 'R.FDB.Incoming'):
        os.makedirs(db(v, d), exist_ok=True)
    write(v, [rec('hqcluster:lab:m', m, 'master', db(v, 'M.FDB.ReplLog'), db(v, 'M.FDB.LogArch'), timeout, other=master_other),
              rec('hqcluster:lab:r', r, 'replica', '', db(v, 'R.FDB.Incoming'), other=replica_other, auth=auth)])
