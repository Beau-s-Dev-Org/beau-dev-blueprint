#!/usr/bin/env bash
#
# Tests the "Fail if the review only partially ran" step of the reusable OCR
# workflow.
#
# That step is the only thing standing between a partial review and a green
# check, and a green check is what every repo's merge policy reads as approval.
# It runs only inside a GitHub job, so without this it would be verified once
# by hand and never again — the shape of dormant guard the step itself exists
# to remove.
#
# The step's shell body is extracted from the workflow rather than duplicated
# here, so the thing under test is the thing that ships. Run:  bash scripts/test-ocr-completeness.sh
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORKFLOW="$ROOT/.github/workflows/ocr-review.yml"
STEP_NAME="Fail if the review only partially ran"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

python3 - "$WORKFLOW" "$STEP_NAME" "$WORK/step.sh" <<'PY'
import sys, yaml
workflow, step_name, out = sys.argv[1], sys.argv[2], sys.argv[3]
doc = yaml.safe_load(open(workflow))
steps = [s for j in doc["jobs"].values() for s in j.get("steps", [])]
match = [s for s in steps if s.get("name") == step_name]
if len(match) != 1:
    sys.exit(f"expected exactly one step named {step_name!r}, found {len(match)}")
open(out, "w").write("#!/bin/bash\n" + match[0]["run"])
PY
[ -s "$WORK/step.sh" ] || { echo "FAIL: could not extract the step"; exit 1; }

# gh must not actually post from a test run.
mkdir -p "$WORK/bin"
printf '#!/bin/sh\necho "[gh] $*" >> "%s/gh.log"\nexit 0\n' "$WORK" > "$WORK/bin/gh"
chmod +x "$WORK/bin/gh"

PASS=0; FAIL=0
run_case() {  # name, expected_exit, result-json-or-NONE, expected substring
  local name="$1" want="$2" json="$3" needle="$4"
  : > "$WORK/summary.md"; : > "$WORK/gh.log"
  if [ "$json" = "NONE" ]; then rm -f /tmp/ocr-result.json; else printf '%s' "$json" > /tmp/ocr-result.json; fi
  local out rc
  out=$(PATH="$WORK/bin:$PATH" GH_TOKEN=x PR_NUMBER=1 REPO=o/r RUN_URL=http://run \
        GITHUB_STEP_SUMMARY="$WORK/summary.md" bash "$WORK/step.sh" 2>&1); rc=$?
  local combined="$out
$(cat "$WORK/summary.md")"
  if [ -z "$needle" ]; then
    echo "  FAIL  $name (empty needle — grep -qF \"\" matches anything)"
    FAIL=$((FAIL+1)); return
  fi
  if [ "$rc" = "$want" ] && printf '%s' "$combined" | grep -qF "$needle"; then
    echo "  ok    $name"; PASS=$((PASS+1))
  else
    echo "  FAIL  $name (exit $rc, wanted $want; looking for: $needle)"
    printf '%s\n' "$out" | sed 's/^/        /'
    FAIL=$((FAIL+1))
  fi
}

COVERAGE='"selected":[{"path":"a.py"},{"path":"b.py"},{"path":"c.py"}],"reused":[],"waived":[]'
PARTIAL='{"status":"partial","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"partial","coverage":{'"$COVERAGE"',"completed":[{"path":"a.py"}],"failed":[{"path":"b.py","classification":"timeout","reason":"file review exceeded its time limit"},{"path":"c.py","classification":"error"}]}}}'
COMPLETE='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"completed","coverage":{'"$COVERAGE"',"completed":[{"path":"a.py"},{"path":"b.py"},{"path":"c.py"}],"failed":[]}}}'
SPACES='{"status":"partial","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"partial","coverage":{'"$COVERAGE"',"completed":[],"failed":[{"path":"docs/my notes.md","classification":"timeout"}]}}}'
# terminal_state says partial while the failed list is empty: either signal alone must fire.
TERMINAL_ONLY='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"partial","coverage":{'"$COVERAGE"',"completed":[{"path":"a.py"}],"failed":[]}}}'
FAILED_ONLY='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"completed","coverage":{'"$COVERAGE"',"completed":[{"path":"a.py"}],"failed":[{"path":"b.py","classification":"timeout"}]}}}'
NEW_SCHEMA='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v2"}}'

echo "OCR completeness gate:"
run_case "a partial review fails the job"          1 "$PARTIAL"       "A partial review is not an approval"
run_case "  ...and names every unreviewed file"    1 "$PARTIAL"       'OCR did not review `c.py`'
run_case "  ...and reports the reason"             1 "$PARTIAL"       "file review exceeded its time limit"
run_case "  ...and says how many were covered"     1 "$PARTIAL"       "1 of 3"
run_case "a complete review passes"                0 "$COMPLETE"      "reviewed every item it selected"
run_case "terminal_state alone is enough"          1 "$TERMINAL_ONLY" "A partial review is not an approval"
run_case "a failed item alone is enough"           1 "$FAILED_ONLY"   "A partial review is not an approval"
run_case "a path with spaces survives"             1 "$SPACES"        'docs/my notes.md'
run_case "a missing result warns, does not block"  0 NONE             "could not be verified"
run_case "an unknown schema warns, does not block" 0 "$NEW_SCHEMA"    "ocr.run-manifest/v2"

# The PR comment is a distinct channel from the annotations; check it moved.
: > "$WORK/gh.log"; printf '%s' "$PARTIAL" > /tmp/ocr-result.json
PATH="$WORK/bin:$PATH" GH_TOKEN=x PR_NUMBER=1 REPO=o/r RUN_URL=http://run \
  GITHUB_STEP_SUMMARY="$WORK/summary.md" bash "$WORK/step.sh" >/dev/null 2>&1
if grep -q "pr comment" "$WORK/gh.log"; then
  echo "  ok    the PR is told, not just the log"; PASS=$((PASS+1))
else
  echo "  FAIL  no PR comment was attempted"; FAIL=$((FAIL+1))
fi

rm -f /tmp/ocr-result.json
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
