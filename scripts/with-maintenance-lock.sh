#!/bin/sh
# Serialize supported writer/lifecycle commands with backup and restore.
set -eu

project_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$project_root"
. ./scripts/maintenance-common.sh
[ "$#" -gt 0 ] || pandect_fail 'Pass the command and arguments to run.'
umask 077
pandect_lock
# Deliberately do not exec: the EXIT trap must release our lock afterward.
"$@"
