#!/usr/bin/env bash
#
# Runs the local-review unit tests (cross-family routing and the "clean"
# verdict, BEA-591) and fails unless every test module actually ran.
#
# On Python 3.11, `unittest discover` exits 0 when it finds no tests, so a
# renamed test module would turn this into a gate that runs nothing.
#
# Run:  bash scripts/test-local-review.sh
set -uo pipefail
shopt -s nullglob

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT

# pipefail keeps a real test failure from being masked by tee.
if ! python3 -m unittest discover -s "$ROOT/scripts/local-review" -v 2>&1 | tee "$LOG"; then
  echo "::error::unittest discover reported failures in scripts/local-review."
  exit 1
fi

# Unanchored: a test that writes output without a trailing newline runs it
# into the summary line, and an anchor would then miss it.
grep -Eq 'Ran [1-9][0-9]* tests? in ' "$LOG" || {
  echo "::error::unittest discover ran no tests in scripts/local-review; the suite did not load."
  exit 1
}

# Per module too: a renamed module leaves the others still printing
# "Ran N tests", and the gate green on a partial suite. Every module must have
# run: the known ones by name, so a module renamed out of discover's pattern or
# deleted still fails here, plus whatever test*.py exists, so a module added
# later is covered too. nullglob: an empty glob adds nothing, never "test*".
# No content-based skipping: a module that lost its tests must fail.
# Shared helpers are named lr_*.py (lr_testkit.py), never test*.py.
expected="test_detect_author test_route test_verdict"
for path in "$ROOT"/scripts/local-review/test*.py; do
  expected="$expected $(basename "$path" .py)"
done
missing=
for module in $(printf '%s\n' $expected | sort -u); do
  grep -qF "($module." "$LOG" || missing="$missing $module"
done
[ -z "$missing" ] || {
  echo "::error::unittest discover did not run$missing; the gate would pass on a partial suite."
  exit 1
}
echo "Local review tests: all modules ran."
