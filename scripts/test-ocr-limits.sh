#!/usr/bin/env bash
#
# Tests the "Check review time limits" step of the reusable OCR workflow.
#
# That step refuses caller-stub limits that do not nest (a request longer than
# its file's task deadline, or a task deadline two waves of which cannot fit
# the attempt step), which would otherwise get the step killed with no review
# report. Like test-ocr-completeness.sh, the step body is extracted from the
# workflow, so the thing under test is the thing that ships.
#
# Run:  bash scripts/test-ocr-limits.sh
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

python3 - "$ROOT/.github/workflows/ocr-review.yml" "$WORK/step.sh" "$WORK/defaults" <<'PY'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
inputs = (doc.get("on") or doc.get(True))["workflow_call"]["inputs"]
open(sys.argv[3], "w").write(f'{inputs["llm_timeout_sec"]["default"]} {inputs["review_task_timeout_min"]["default"]}\n')
steps = [s for j in doc["jobs"].values() for s in j.get("steps", [])]
match = [s for s in steps if s.get("name") == "Check review time limits"]
if len(match) != 1:
    sys.exit(f"expected exactly one 'Check review time limits' step, found {len(match)}")
open(sys.argv[2], "w").write(match[0]["run"])
# The run body is tested below with injected values; this checks the wiring
# that feeds those values in production, which the body test cannot see.
want = {
    "LLM_TIMEOUT_SEC": "${{ inputs.llm_timeout_sec }}",
    "TASK_TIMEOUT_MIN": "${{ inputs.review_task_timeout_min }}",
    "CHANGED_FILES": "${{ github.event_name == 'issue_comment' && steps.pr-context.outputs.changed_files || github.event.pull_request.changed_files }}",
}
env = match[0].get("env") or {}
for k, v in want.items():
    if env.get(k) != v:
        sys.exit(f"env {k} is {env.get(k)!r}, expected {v!r}")
ctx = [s for s in steps if s.get("id") == "pr-context"]
if len(ctx) != 1 or "core.setOutput('changed_files'" not in ctx[0].get("with", {}).get("script", ""):
    sys.exit("the pr-context step no longer outputs changed_files")
# By id, so an attempt that LOST these inputs fails too, not only one with
# wrong values.
attempts = [s for s in steps if s.get("id") in ("ocr-primary", "ocr-fallback1", "ocr-fallback2")]
if len(attempts) != 3:
    sys.exit(f"expected 3 review attempt steps, found {len(attempts)}")
for s in attempts:
    w = s.get("with") or {}
    if (w.get("review_task_timeout") != "${{ steps.limits.outputs.task_minutes }}"
            or w.get("llm_timeout") != "${{ steps.limits.outputs.llm_timeout_sec }}"
            or w.get("effort") != "${{ steps.limits.outputs.effort }}"):
        sys.exit(f"attempt {s.get('name')!r} is not fed by the limits step's outputs")
PY
# The wiring assertions above end in sys.exit AFTER step.sh is written, and
# this script has no -e, so the status must be checked explicitly or a wiring
# regression would leave the suite green.
[ "$?" -eq 0 ] || { echo "FAIL: extraction or workflow-wiring checks failed"; exit 1; }
[ -s "$WORK/step.sh" ] || { echo "FAIL: could not extract the step"; exit 1; }

pass=0; fail=0
# $1 expected exit, $2 llm seconds, $3 task minutes, $4 changed files,
# $5 label, $6 text the output must contain, $7 expected "task llm" outputs,
# $8 text the output must NOT contain.
# A rejection must show ITS message: an unrelated exit 1 (an unset variable,
# a typo in a test) would otherwise satisfy every rejection case.
check() {
  : > "$WORK/gh_out"
  LLM_TIMEOUT_SEC="$2" TASK_TIMEOUT_MIN="$3" CHANGED_FILES="$4" GITHUB_OUTPUT="$WORK/gh_out" \
    bash --noprofile --norc -eo pipefail "$WORK/step.sh" >"$WORK/out" 2>&1   # as Actions runs it
  local got=$? why=""
  [ "$got" -eq "$1" ] || why="expected exit $1, got $got"
  [ -z "$why" ] && [ -n "${6:-}" ] && ! grep -qF -- "$6" "$WORK/out" && why="output lacks: $6"
  [ -z "$why" ] && [ -n "${8:-}" ] && grep -qF -- "$8" "$WORK/out" && why="output contains: $8"
  if [ -z "$why" ] && [ -n "${7:-}" ]; then
    local t l
    t=$(sed -n 's/^task_minutes=//p' "$WORK/gh_out"); l=$(sed -n 's/^llm_timeout_sec=//p' "$WORK/gh_out")
    [ "$t $l" = "$7" ] || why="outputs '$t $l', expected '$7'"
  fi
  if [ -z "$why" ]; then pass=$((pass+1)); echo "  ok    $5"
  else fail=$((fail+1)); echo "  FAIL  $5 ($why)"; sed 's/^/        /' "$WORK/out"; fi
}

read -r DEF_LLM DEF_TASK < "$WORK/defaults"
check 0 "$DEF_LLM" "$DEF_TASK" 3 "shipped defaults pass (3 files) ($DEF_LLM s, $DEF_TASK min)" "" "$DEF_TASK $DEF_LLM"

# Per-file deadline = min(configured, floor(50 / (waves of 8 * 2 rounds
#   + 1 grouping request from 4 files))).
check 0 1200 25 1   "1 file: one wave, no grouping, full deadline"  "1 wave(s) x 2 rounds" "25 1200"
check 0 1200 25 3   "3 files: still no grouping, full deadline"     "1 wave(s)" "25 1200"
check 0 1200 25 4   "4 files: grouping request counted, deadline 16" "deadline is 16 min" "16 960"
check 0 1200 25 8   "8 files: deadline 16"                          "1 wave(s)" "16 960"
check 0 1200 25 9   "9 files: two waves, deadline 10"               "2 waves" "10 600"
check 0 1200 25 16  "16 files: deadline 10"                         "2 waves" "10 600"
check 0 1200 25 17  "17 files: three waves, deadline 7"             "3 waves" "7 420"
check 0 1200 25 65  "65 files: requests under 300s get a warning"  "below OCR's own 300s" "2 120"
check 0 1200 25 40  "40 files: five waves, deadline 4"              "5 waves" "4 240"
check 0 1200 25 192 "192 files: 24 waves, deadline 1"               "24 waves" "1 60"
check 0 600  10 2   "a lower configured deadline is kept"           "" "10 600"
check 0 300  25 11  "a request below the deadline is kept"          "" "10 300"
check 0 1200 25 0   "0 files counts as one wave"                    "1 wave(s)" "25 1200"

# Which limits were shortened, for the notice's advice.
flags() {  # $1 llm, $2 task, $3 files, $4 label, $5 expected "task llm" flags
  : > "$WORK/gh_out"
  LLM_TIMEOUT_SEC="$1" TASK_TIMEOUT_MIN="$2" CHANGED_FILES="$3" GITHUB_OUTPUT="$WORK/gh_out" \
    bash --noprofile --norc -eo pipefail "$WORK/step.sh" >"$WORK/out" 2>&1
  local got
  got="$(sed -n 's/^task_shortened=//p' "$WORK/gh_out") $(sed -n 's/^llm_shortened=//p' "$WORK/gh_out")"
  if [ "$got" = "$5" ]; then pass=$((pass+1)); echo "  ok    $4"
  else fail=$((fail+1)); echo "  FAIL  $4 (flags '$got', expected '$5')"; fi
}
flags 1200 25 2  "nothing shortened on a small PR"              "false false"
flags 300  25 11 "deadline shortened, short request kept"       "true false"
flags 1200 25 11 "deadline and request both shortened"          "true true"

: > "$WORK/gh_out"
LLM_TIMEOUT_SEC=1200 TASK_TIMEOUT_MIN=25 CHANGED_FILES=1 GITHUB_OUTPUT="$WORK/gh_out" \
  bash --noprofile --norc -eo pipefail "$WORK/step.sh" >"$WORK/out" 2>&1
if grep -qx "effort=medium" "$WORK/gh_out"; then
  pass=$((pass+1)); echo "  ok    effort is pinned to medium"
else
  fail=$((fail+1)); echo "  FAIL  effort output is not 'medium'"; sed 's/^/        /' "$WORK/out"
fi

check 0 1200 25 201 "201 files: 1-minute floor with a warning, review still runs" "::warning::" "1 60"
check 0 1200 25 5000 "5000 files: still runs at the floor"         "Splitting the PR is the fix" "1 60"
check 1 1200 25 ""  "rejects an unknown file count"                "Could not read the PR's changed-file count"
check 1 1200 25 "x" "rejects a non-numeric file count"             "Could not read the PR's changed-file count"
check 0 1501 25 3   "a request longer than its deadline is clamped, with a warning" "it is clamped to that deadline" "25 1500"
check 0 1200 26 3   "a deadline over 25 is clamped, with a warning" "using at most 25" "25 1200"
check 0 3000 30 3   "an older stub with higher settings still gets a review" "::warning::" "25 1500"

# A failure also writes its reason as an output, newline-free.
: > "$WORK/gh_out"
LLM_TIMEOUT_SEC=$'12\ninjected=1' TASK_TIMEOUT_MIN=25 CHANGED_FILES=3 GITHUB_OUTPUT="$WORK/gh_out" \
  bash --noprofile --norc -eo pipefail "$WORK/step.sh" >"$WORK/out" 2>&1
if [ "$(grep -c '^error=' "$WORK/gh_out")" = 1 ] && ! grep -q '^injected=' "$WORK/gh_out"; then
  pass=$((pass+1)); echo "  ok    a failure's reason is one output line, newlines stripped"
else
  fail=$((fail+1)); echo "  FAIL  error output missing or injectable"; sed 's/^/        /' "$WORK/gh_out"
fi
check 1 1200 0  11  "rejects a zero deadline"                    "TASK_TIMEOUT_MIN must be a positive integer"
check 1 0    25 11  "rejects a zero request timeout"             "LLM_TIMEOUT_SEC must be a positive integer"
check 1 ""   25 11  "rejects an empty request timeout"           "LLM_TIMEOUT_SEC must be a positive integer"
check 1 1200 ""  11 "rejects an empty deadline"                  "TASK_TIMEOUT_MIN must be a positive integer"
check 1 "12a" 25 11 "rejects a non-numeric request timeout"      "LLM_TIMEOUT_SEC must be a positive integer"
check 1 1200 "2.5" 11 "rejects a fractional deadline"            "TASK_TIMEOUT_MIN must be a positive integer"
check 1 "-5" 25 11  "rejects a negative request timeout"         "LLM_TIMEOUT_SEC must be a positive integer"
# The value is echoed back inside quotes, so "$(echo" appears; an executed
# substitution would print the bare sentinel, which must never appear.
check 1 '$(echo INJECTED_SENTINEL_9f3)' 25 11 "treats a command substitution as data" \
  "must be a positive integer" "" "INJECTED_SENTINEL_9f3'."
check 1 '`echo INJECTED_SENTINEL_9f3`' 25 11 "treats backticks as data" \
  "must be a positive integer" "" "INJECTED_SENTINEL_9f3'."

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
