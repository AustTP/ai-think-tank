"""Collection guard for the world tree.

.selfedit_staging / .selfedit_failed hold self-edit tooling snapshots: a
copy of the whole tree (tests included) without its dependencies
installed. pytest must never collect them -- a snapshot appearing mid-run
turns every module in it into an ImportError and aborts the whole session
(451 collection errors in one 7-minute run).
"""
collect_ignore = ['.selfedit_staging', '.selfedit_failed']
