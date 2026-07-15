"""Shared engine constants.

Subprocess timeouts used by the protocol engine and its extracted mixin
modules (optimize_completion.py). Kept in a standalone module so the mixin
files can import them without importing protocol_engine (which would create
an import cycle, since protocol_engine imports the mixins).
"""

# Git subprocess timeouts (seconds). Windows git can be slow due to
# antivirus, credential helpers, and filesystem overhead.
GIT_TIMEOUT_FAST = 15   # branch, rev-parse, status, symbolic-ref
GIT_TIMEOUT_MEDIUM = 30  # checkout, add, commit, merge, tag
GIT_TIMEOUT_SLOW = 60   # push, fetch, clone
TEST_TIMEOUT = 600       # pytest/jest/cargo test — can be long on large suites
