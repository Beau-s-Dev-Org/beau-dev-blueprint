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

SEL3='"selected":[{"item_id":"1","path":"a.py"},{"item_id":"2","path":"b.py"},{"item_id":"3","path":"c.py"}]'
V1='"schema_version":"ocr.run-manifest/v1"'

mk() {  # status, terminal_state, completed, failed, reused, waived, selected
  printf '{"status":"%s","manifest":{%s,"terminal_state":"%s","coverage":{%s,"completed":%s,"failed":%s,"reused":%s,"waived":%s}}}' \
    "$1" "$V1" "$2" "${7:-$SEL3}" "$3" "$4" "$5" "$6"
}
ALL3='[{"item_id":"1","path":"a.py"},{"item_id":"2","path":"b.py"},{"item_id":"3","path":"c.py"}]'
ONE='[{"item_id":"1","path":"a.py"}]'
FAIL2='[{"item_id":"2","path":"b.py","classification":"timeout","reason":"file review exceeded its time limit"},{"item_id":"3","path":"c.py","classification":"error"}]'

PARTIAL=$(mk partial partial "$ONE" "$FAIL2" '[]' '[]')
COMPLETE=$(mk success completed "$ALL3" '[]' '[]' '[]')
TERMINAL_ONLY=$(mk success partial "$ALL3" '[]' '[]' '[]')
STATUS_ONLY=$(mk partial completed "$ALL3" '[]' '[]' '[]')
FAILED_ONLY=$(mk success completed "$ONE" "$FAIL2" '[]' '[]')
# Devin: a "skipped" run that still selected files reviewed nothing.
SKIPPED_WITH_SEL=$(mk skipped skipped '[]' '[]' '[]' '[]')
# ...but a run that selected nothing had nothing to review.
SKIPPED_EMPTY=$(mk skipped skipped '[]' '[]' '[]' '[]' '"selected":[]')
# reused/waived are covered, not gaps: 1 completed + 1 reused + 1 waived = all 3.
REUSED_WAIVED=$(mk success completed "$ONE" '[]' '[{"item_id":"2","path":"b.py"}]' '[{"item_id":"3","path":"c.py"}]')
# ...and when one DID fail, the count must agree with the listed paths.
MIXED=$(mk partial partial "$ONE" '[{"item_id":"3","path":"c.py","classification":"timeout"}]' '[{"item_id":"2","path":"b.py"}]' '[]')
# An item in no bucket at all is a gap the failed list cannot describe.
UNACCOUNTED=$(mk success completed "$ONE" '[]' '[]' '[]')
# An empty selection must not outrank the explicit signals.
EMPTY_BUT_PARTIAL=$(mk partial partial '[]' '[]' '[]' '[]' '"selected":[]')
EMPTY_BUT_FAILED=$(mk success completed '[]' '[{"item_id":"9","path":"x.py","classification":"timeout"}]' '[]' '[]' '"selected":[]')
SPACES=$(mk partial partial '[]' '[{"item_id":"1","path":"docs/my notes.md","classification":"timeout"}]' '[]' '[]' '"selected":[{"item_id":"1","path":"docs/my notes.md"}]')
NEW_SCHEMA='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v2"}}'
# A v2 manifest that KEPT the v1 arrays: the array-type guard alone let this
# through as a full review while its own message claimed only v1 was understood.
V2_WITH_ARRAYS='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v2","terminal_state":"completed","coverage":{"selected":[{"item_id":"1","path":"a.py"}],"completed":[{"item_id":"1","path":"a.py"}],"failed":[],"reused":[],"waived":[]}}}'
NO_SCHEMA='{"status":"success","manifest":{"terminal_state":"completed","coverage":{"selected":[],"completed":[],"failed":[],"reused":[],"waived":[]}}}'
# A v1 manifest whose coverage is malformed: the schema check passes it, so
# this is the only thing that reaches the array-shape guard. Without it that
# guard is unreachable in the suite and survives being deleted.
V1_BAD_COVERAGE='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"completed","coverage":{}}}'
V1_COVERAGE_NOT_ARRAYS='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"completed","coverage":{"selected":"a.py","completed":[],"failed":[]}}}'
# A v2 result that still has a coverage OBJECT must not sneak through.
V2_EMPTY_COVERAGE='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v2","coverage":{}}}'

echo "OCR completeness gate:"
run_case "a partial review fails the job"          1 "$PARTIAL"           "A partial review is not an approval"
run_case "  ...and names every unreviewed file"    1 "$PARTIAL"           'OCR did not review `c.py`'
run_case "  ...and reports the reason"             1 "$PARTIAL"           "file review exceeded its time limit"
run_case "  ...and says how much was covered"      1 "$PARTIAL"           "1 of 3"
run_case "a complete review passes"                0 "$COMPLETE"          "reviewed every item it selected"
run_case "terminal_state alone is enough"          1 "$TERMINAL_ONLY"     "A partial review is not an approval"
run_case "status alone is enough"                  1 "$STATUS_ONLY"       "A partial review is not an approval"
run_case "a failed item alone is enough"           1 "$FAILED_ONLY"       "A partial review is not an approval"
run_case "a path with spaces survives"             1 "$SPACES"            'docs/my notes.md'
run_case "skipped WITH selected files fails"       1 "$SKIPPED_WITH_SEL"  "A partial review is not an approval"
run_case "  ...and names them as unaccounted"      1 "$SKIPPED_WITH_SEL"  "not reported in any coverage bucket"
run_case "selecting nothing is not a failure"      0 "$SKIPPED_EMPTY"     "selected no files for review"
run_case "reused and waived count as covered"      0 "$REUSED_WAIVED"     "reviewed every item it selected"
run_case "the count agrees with the listed paths"  1 "$MIXED"             "2 of 3"
run_case "an item in no bucket is a gap"           1 "$UNACCOUNTED"       "not reported in any coverage bucket"
run_case "a missing result warns, does not block"  0 NONE                 "could not be verified"
run_case "an unknown schema warns, does not block" 0 "$NEW_SCHEMA"        "ocr.run-manifest/v2"
run_case "an empty coverage object cannot pass"    0 "$V2_EMPTY_COVERAGE" "could not be verified"
run_case "a v2 schema keeping v1 arrays abstains"  0 "$V2_WITH_ARRAYS"    "unrecognised result schema"
run_case "a manifest with no schema abstains"     0 "$NO_SCHEMA"         "unrecognised result schema"
run_case "a v1 result with empty coverage abstains" 0 "$V1_BAD_COVERAGE"  "coverage arrays missing or malformed"
run_case "a v1 result with a non-array abstains"   0 "$V1_COVERAGE_NOT_ARRAYS" "coverage arrays missing or malformed"
run_case "partial outranks an empty selection"    1 "$EMPTY_BUT_PARTIAL" "A partial review is not an approval"
run_case "  ...and admits it named nothing"       1 "$EMPTY_BUT_PARTIAL" "without naming which items"
run_case "a failed item outranks an empty set"    1 "$EMPTY_BUT_FAILED"  'OCR did not review `x.py`'

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
