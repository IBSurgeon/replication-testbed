"""Test bed tests. Each module here is one `tb.py test <name>` command.

A test module defines:
  HELP              one line for `tb.py test list`
  add_args(parser)  its own options
  run(cluster, args) -> exit code (0 = all cases passed)

Put shared steps in tblib/, not here. See docs/adding-tests.md.
"""
