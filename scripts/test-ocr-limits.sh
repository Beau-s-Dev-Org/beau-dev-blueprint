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
check() {  # $1 expected exit (0|1), $2 llm seconds, $3 task minutes, $4 label
  LLM_TIMEOUT_SEC="$2" TASK_TIMEOUT_MIN="$3" bash "$WORK/step.sh" >"$WORK/out" 2>&1
  local got=$?
  if [ "$got" -eq "$1" ]; then pass=$((pass+1)); echo "  ok    $4"
  else fail=$((fail+1)); echo "  FAIL  $4 (expected exit $1, got $got)"; sed 's/^/        /' "$WORK/out"; fi
}

read -r DEF_LLM DEF_TASK < "$WORK/defaults"
check 0 "$DEF_LLM" "$DEF_TASK" "the shipped defaults pass ($DEF_LLM s, $DEF_TASK min)"
check 0 1500 25 "a request equal to the task deadline passes"
check 0 60   1  "the smallest values pass"
check 1 1501 25 "rejects a request longer than its task deadline"
check 1 1200 26 "rejects a task deadline over 25 minutes"
check 1 1200 0  "rejects a zero task deadline"
check 1 0    25 "rejects a zero request timeout"
check 1 ""   25 "rejects an empty request timeout"
check 1 1200 "" "rejects an empty task deadline"
check 1 "12a" 25 "rejects a non-numeric request timeout"
check 1 1200 "2.5" "rejects a fractional task deadline"
check 1 "-5" 25 "rejects a negative request timeout"
check 1 '$(echo pwned)' 25 "treats a command substitution as data, not code"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
