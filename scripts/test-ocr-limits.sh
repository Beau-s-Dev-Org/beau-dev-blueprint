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
PY
[ -s "$WORK/step.sh" ] || { echo "FAIL: could not extract the step"; exit 1; }

pass=0; fail=0
# $1 expected exit, $2 llm seconds, $3 task minutes, $4 changed files,
# $5 label, $6 text the output must contain, $7 expected "task llm" outputs.
# A rejection must show ITS message: an unrelated exit 1 (an unset variable,
# a typo in a test) would otherwise satisfy every rejection case.
check() {
  : > "$WORK/gh_out"
  LLM_TIMEOUT_SEC="$2" TASK_TIMEOUT_MIN="$3" CHANGED_FILES="$4" GITHUB_OUTPUT="$WORK/gh_out" \
    bash "$WORK/step.sh" >"$WORK/out" 2>&1
  local got=$? why=""
  [ "$got" -eq "$1" ] || why="expected exit $1, got $got"
  [ -z "$why" ] && [ -n "${6:-}" ] && ! grep -qF -- "$6" "$WORK/out" && why="output lacks: $6"
  if [ -z "$why" ] && [ -n "${7:-}" ]; then
    local t l
    t=$(sed -n 's/^task_minutes=//p' "$WORK/gh_out"); l=$(sed -n 's/^llm_timeout_sec=//p' "$WORK/gh_out")
    [ "$t $l" = "$7" ] || why="outputs '$t $l', expected '$7'"
  fi
  if [ -z "$why" ]; then pass=$((pass+1)); echo "  ok    $5"
  else fail=$((fail+1)); echo "  FAIL  $5 ($why)"; sed 's/^/        /' "$WORK/out"; fi
}

read -r DEF_LLM DEF_TASK < "$WORK/defaults"
check 0 "$DEF_LLM" "$DEF_TASK" 8 "shipped defaults pass (8 files) ($DEF_LLM s, $DEF_TASK min)" "" "$DEF_TASK $DEF_LLM"

# Per-file deadline = min(configured, floor(50 / (waves of 8 * 2 rounds))).
check 0 1200 25 1   "1 file: one wave, full deadline"              "1 wave(s) x 2 rounds" "25 1200"
check 0 1200 25 8   "8 files: one wave, full deadline"             "1 wave(s)" "25 1200"
check 0 1200 25 9   "9 files: two waves, deadline 12"              "deadline is 12 min" "12 720"
check 0 1200 25 16  "16 files: two waves, deadline 12"             "2 waves" "12 720"
check 0 1200 25 17  "17 files: three waves, deadline 8"            "3 waves" "8 480"
check 0 1200 25 40  "40 files: five waves, deadline 5"             "5 waves" "5 300"
check 0 1200 25 200 "200 files: 25 waves, deadline 1"              "25 waves" "1 60"
check 0 600  10 11  "a lower configured deadline is kept"          "" "10 600"
check 0 300  25 11  "a request below the deadline is kept"         "" "12 300"
check 0 1200 25 0   "0 files counts as one wave"                   "1 wave(s)" "25 1200"
check 0 1200 25 1   "effort is pinned to medium"                   "" ""
grep -qx "effort=medium" "$WORK/gh_out" || { fail=$((fail+1)); echo "  FAIL  effort output is not 'medium'"; }

check 1 1200 25 201 "rejects a PR too large for even 1 min/file"   "Split the PR"
check 1 1200 25 ""  "rejects an unknown file count"                "Could not read the PR's changed-file count"
check 1 1200 25 "x" "rejects a non-numeric file count"             "Could not read the PR's changed-file count"
check 1 1501 25 11  "rejects a request longer than its deadline" "longer than the 25-minute"
check 1 1200 26 11  "rejects a deadline over 25 minutes"         "the maximum is 25"
check 1 1200 0  11  "rejects a zero deadline"                    "TASK_TIMEOUT_MIN must be a positive integer"
check 1 0    25 11  "rejects a zero request timeout"             "LLM_TIMEOUT_SEC must be a positive integer"
check 1 ""   25 11  "rejects an empty request timeout"           "LLM_TIMEOUT_SEC must be a positive integer"
check 1 1200 ""  11 "rejects an empty deadline"                  "TASK_TIMEOUT_MIN must be a positive integer"
check 1 "12a" 25 11 "rejects a non-numeric request timeout"      "LLM_TIMEOUT_SEC must be a positive integer"
check 1 1200 "2.5" 11 "rejects a fractional deadline"            "TASK_TIMEOUT_MIN must be a positive integer"
check 1 "-5" 25 11  "rejects a negative request timeout"         "LLM_TIMEOUT_SEC must be a positive integer"
check 1 '$(echo pwned)' 25 11 "treats a command substitution as data" "must be a positive integer"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
