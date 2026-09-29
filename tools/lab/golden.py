#!/usr/bin/env python3
"""Turns a lab copy's replication.log into a golden log for the node's
parser tests: lab paths and the host name replaced, repeated idle blocks
dropped, LF line ends.

    python tools/lab/golden.py 30|25 OUT_FILE
"""
import os
import re
import socket
import sys

import lab

BS = chr(92)


def golden(src, dst):
    raw = open(src, 'rb').read().decode('utf-8', 'replace')
    raw = raw.replace(lab.DIR, 'C:' + BS + 'lab').replace(lab.DIR.replace(BS, '/'), 'C:/lab')
    raw = raw.replace(socket.gethostname().upper(), 'LABHOST').replace(socket.gethostname(), 'LABHOST')
    blocks = re.split(r'\r?\n\r?\n', raw)
    out, idle = [], 0
    for b in blocks:
        if not b.strip():
            continue
        if 'No new segments found' in b:
            idle += 1
            if idle > 1:
                continue
        else:
            idle = 0
        out.append(b.strip('\r\n'))
    text = '\n\n'.join(out) + '\n\n'
    open(dst, 'w', newline='\n').write(text.replace('\r\n', '\n'))
    print(dst, len(out), 'blocks', len(text), 'bytes')


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in lab.VERSIONS:
        sys.exit(__doc__)
    golden(os.path.join(lab.root(sys.argv[1]), 'replication.log'), sys.argv[2])
