#!/usr/bin/env bash
# Run the tests.
#
#   ./test.sh            everything
#   ./test.sh cron       one file
#   ./test.sh -v         with each test named
#
# No dependencies: unittest is in the standard library, and there is no pip on this laptop.
# ResourceWarning is silenced because one test deliberately orphans a process to check it is
# adopted afterwards, and Python is right to complain about that.

set -u
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here" || exit 1

first="${1:-}"
if [ -n "$first" ] && [ "${first#-}" = "$first" ]; then
    shift
    exec python3 -W "ignore::ResourceWarning" -m unittest "tests.test_${first}" "$@"
fi
exec python3 -W "ignore::ResourceWarning" -m unittest discover -s tests -t . "$@"
