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
cat > "$WORK/bin/gh" <<GHEOF
#!/bin/sh
echo "[gh] \$*" >> "$WORK/gh.log"
# When OCR_FAKE_PRIOR_COMMENT is set, the marker lookup finds an existing
# comment, so the clear/update path is exercised instead of short-circuiting.
case "\$*" in
  *issues/*/comments*--paginate*) [ -n "\${OCR_FAKE_PRIOR_COMMENT:-}" ] && echo 4242 ;;
esac
exit 0
GHEOF
chmod +x "$WORK/bin/gh"

PASS=0; FAIL=0
run_case() {  # name, expected_exit, result-json-or-NONE, expected substring
  local name="$1" want="$2" json="$3" needle="$4"
  : > "$WORK/summary.md"; : > "$WORK/gh.log"
  # Hermetic: the step honours OCR_RESULT_FILE, so fixtures live in this run's
  # own temp dir. A fixed /tmp path is racy between concurrent runs and, on a
  # shared host, `rm -f` cannot clear another user's pre-planted symlink — the
  # write would then follow it.
  if [ "$json" = "NONE" ]; then rm -f "$WORK/result.json"; else printf '%s' "$json" > "$WORK/result.json"; fi
  local out rc
  # --noprofile --norc -eo pipefail is exactly how Actions runs a `shell: bash`
  # step, so -e is ON there and was OFF here. The step tolerates -e today, but
  # nothing pinned that: a future line failing benignly would abort the real job
  # while this suite stayed green, breaking the promise that it tests what ships.
  out=$(PATH="$WORK/bin:$PATH" GH_TOKEN=x PR_NUMBER=1 REPO=o/r RUN_URL=http://run \
        OCR_RESULT_FILE="$WORK/result.json" OCR_COMMENT_FILE="$WORK/comment.md" \
        GITHUB_STEP_SUMMARY="$WORK/summary.md" \
        bash --noprofile --norc -eo pipefail "$WORK/step.sh" 2>&1); rc=$?
  local combined="$out
$(cat "$WORK/summary.md")"
  if [ -z "$needle" ]; then
    echo "  FAIL  $name (empty needle — grep -qF \"\" matches anything)"
    FAIL=$((FAIL+1)); return
  fi
  if [ "$rc" = "$want" ] && printf '%s' "$combined" | grep -qF "$needle"; then
    # gh is only ever called on the incomplete path, so a green or abstaining
    # run that touched it is a regression the exit code alone would miss.
    # A passing run may look up the sticky comment and clear a stale one; it
    # must never POST a new "did not finish". `gh pr comment` is that post.
    if [ "$want" = "0" ] && grep -q "pr comment" "$WORK/gh.log" 2>/dev/null; then
      echo "  FAIL  $name (exited 0 but posted a PR comment: $(cat "$WORK/gh.log"))"
      FAIL=$((FAIL+1)); return
    fi
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
COMPLETE=$(mk success complete "$ALL3" '[]' '[]' '[]')
TERMINAL_ONLY=$(mk success partial "$ALL3" '[]' '[]' '[]')
STATUS_ONLY=$(mk partial complete "$ALL3" '[]' '[]' '[]')
FAILED_ONLY=$(mk success complete "$ONE" "$FAIL2" '[]' '[]')
# Devin: a "skipped" run that still selected files reviewed nothing.
SKIPPED_WITH_SEL=$(mk skipped skipped '[]' '[]' '[]' '[]')
# ...but a run that selected nothing had nothing to review.
SKIPPED_EMPTY=$(mk skipped skipped '[]' '[]' '[]' '[]' '"selected":[]')
# reused/waived are covered, not gaps: 1 completed + 1 reused + 1 waived = all 3.
REUSED_WAIVED=$(mk success complete "$ONE" '[]' '[{"item_id":"2","path":"b.py"}]' '[{"item_id":"3","path":"c.py"}]')
# ...and when one DID fail, the count must agree with the listed paths.
MIXED=$(mk partial partial "$ONE" '[{"item_id":"3","path":"c.py","classification":"timeout"}]' '[{"item_id":"2","path":"b.py"}]' '[]')
# An item in no bucket at all is a gap the failed list cannot describe.
UNACCOUNTED=$(mk success complete "$ONE" '[]' '[]' '[]')
# An empty selection must not outrank the explicit signals.
EMPTY_BUT_PARTIAL=$(mk partial partial '[]' '[]' '[]' '[]' '"selected":[]')
EMPTY_BUT_FAILED=$(mk success complete '[]' '[{"item_id":"9","path":"x.py","classification":"timeout"}]' '[]' '[]' '"selected":[]')
SPACES=$(mk partial partial '[]' '[{"item_id":"1","path":"docs/my notes.md","classification":"timeout"}]' '[]' '[]' '"selected":[{"item_id":"1","path":"docs/my notes.md"}]')
NEW_SCHEMA='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v2"}}'
# A v2 manifest that KEPT the v1 arrays: the array-type guard alone let this
# through as a full review while its own message claimed only v1 was understood.
V2_WITH_ARRAYS='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v2","terminal_state":"complete","coverage":{"selected":[{"item_id":"1","path":"a.py"}],"completed":[{"item_id":"1","path":"a.py"}],"failed":[],"reused":[],"waived":[]}}}'
# Verified against the real thing (run 35836951000): OCR says "complete", not
# "completed", and this suite had guessed. It does not change a verdict — the
# gate matches on "partial" and on coverage arithmetic, never on a success word,
# which is exactly why guessing it wrong cost nothing — but a fixture that does
# not match reality is not evidence about reality.
REAL_SHAPE='{"status":"complete","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"item_id":"aa","path":"a.py"}],"completed":[{"item_id":"aa","path":"a.py"}],"failed":[],"reused":[],"waived":[]}}}'
# Multi-line tool output in a reason: one entry must stay one annotation.
MULTILINE_REASON='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"path":"a.py"}],"completed":[],"failed":[{"path":"a.py","classification":"timeout","reason":"boom\n::error::FORGED\n::add-mask::secret"}],"reused":[],"waived":[]}}}'
# A backtick in a path would close the markdown code span early.
NEWLINE_STATUS='{"status":"success\n::error::FORGED","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"path":"a.py"}],"completed":[],"failed":[],"reused":[],"waived":[]}}}'
BACKTICK_PATH='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"path":"a`b.py"}],"completed":[],"failed":[],"reused":[],"waived":[]}}}'
NO_SCHEMA='{"status":"success","manifest":{"terminal_state":"complete","coverage":{"selected":[],"completed":[],"failed":[],"reused":[],"waived":[]}}}'
# A v1 manifest whose coverage is malformed: the schema check passes it, so
# this is the only thing that reaches the array-shape guard. Without it that
# guard is unreachable in the suite and survives being deleted.
V1_BAD_COVERAGE='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{}}}'
V1_COVERAGE_NOT_ARRAYS='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":"a.py","completed":[],"failed":[]}}}'
# Arrays of the wrong THING: the extraction dies, the redirect leaves an empty
# file, and without `set -e` that reads as "nothing was selected" and exits green.
V1_SELECTED_STRINGS='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":["a.py","b.py"],"completed":[],"failed":[],"reused":[],"waived":[]}}}'
V1_ENTRY_NO_ID='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"fingerprint":"x"}],"completed":[],"failed":[],"reused":[],"waived":[]}}}'
# An empty item_id beside a good path: jq's // only falls back on null/false,
# so the empty string won and the entry vanished from the counts.
V1_EMPTY_ID='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"item_id":"","path":"a.py"}],"completed":[],"failed":[],"reused":[],"waived":[]}}}'
# ...and the same entry, actually reviewed, must still pass.
V1_EMPTY_ID_DONE='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"item_id":"","path":"a.py"}],"completed":[{"item_id":"","path":"a.py"}],"failed":[],"reused":[],"waived":[]}}}'
# Neither identity usable at all.
V1_BOTH_EMPTY='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"item_id":"","path":""}],"completed":[],"failed":[],"reused":[],"waived":[]}}}'
# One entry's path equal to another's item_id: comparing bare strings let them
# alias under sort -u, so two selected items counted as one.
# git permits a newline in a filename. The comparison used to run through
# newline-delimited sort/comm, where one path became two lines.
V1_NEWLINE_PATH='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"path":"a.py"},{"path":"b.py"},{"path":"a.py\nb.py"}],"completed":[{"path":"a.py"},{"path":"b.py"}],"failed":[],"reused":[],"waived":[]}}}'
# The case that actually separates a jq comparison from a line-oriented one:
# a file literally named "path:b.py" inside a newline path. Split into lines,
# its fragments become byte-identical to two real entries and the third item
# disappears -> "2 of 2" and a green check. Compared as values, it stays three.
V1_NEWLINE_ALIAS='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"path":"a.py"},{"path":"b.py"},{"path":"a.py\npath:b.py"}],"completed":[{"path":"a.py"},{"path":"b.py"}],"failed":[],"reused":[],"waived":[]}}}'
# "partial" is a schema-independent fact: it must outrank every abstain path.
PARTIAL_BAD_COVERAGE='{"status":"partial","manifest":{"schema_version":"ocr.run-manifest/v1","coverage":{}}}'
PARTIAL_BAD_SCHEMA='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v9","terminal_state":"partial"}}'
PARTIAL_NOT_JSON='not json at all'
V1_ID_COLLISION='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"item_id":"a.py","path":"first.py"},{"item_id":"","path":"a.py"}],"completed":[{"item_id":"a.py","path":"first.py"}],"failed":[],"reused":[],"waived":[]}}}'
V1_REUSED_STRING='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"complete","coverage":{"selected":[{"path":"a.py"}],"completed":[{"path":"a.py"}],"failed":[],"reused":["b.py"],"waived":[]}}}'
# A v2 result that still has a coverage OBJECT must not sneak through.
V2_EMPTY_COVERAGE='{"status":"success","manifest":{"schema_version":"ocr.run-manifest/v2","coverage":{}}}'

echo "OCR completeness gate:"
run_case "a partial review fails the job"          1 "$PARTIAL"           "A partial review is not an approval"
run_case "  ...and names every unreviewed file"    1 "$PARTIAL"           'OCR did not review `c.py`'
run_case "  ...and reports the reason"             1 "$PARTIAL"           "file review exceeded its time limit"
run_case "  ...and says how much was covered"      1 "$PARTIAL"           "1 of 3"
run_case "a complete review passes"                0 "$COMPLETE"          "reviewed every item it selected"
run_case "the shape OCR actually emits passes"     0 "$REAL_SHAPE"        "reviewed every item it selected"
run_case "a multi-line reason stays one entry"     1 "$MULTILINE_REASON"  'boom\n::error::FORGED' 

run_case "a backtick cannot break the code span"   1 "$BACKTICK_PATH"     'a&#96;b.py' 
run_case "a newline in status stays one line"      1 "$NEWLINE_STATUS"    'success\n::error::FORGED'
run_case "an empty result file abstains"           0 ""                   "could not be verified"
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
run_case "an empty coverage object cannot pass"    0 "$V2_EMPTY_COVERAGE" "ocr.run-manifest/v2"
run_case "a v2 schema keeping v1 arrays abstains"  0 "$V2_WITH_ARRAYS"    "understands ocr.run-manifest/v1 only"
run_case "a manifest with no schema abstains"     0 "$NO_SCHEMA"         "schema_version absent"
run_case "a v1 result with empty coverage abstains" 0 "$V1_BAD_COVERAGE"  "coverage arrays are missing or hold entries"
run_case "a v1 result with a non-array abstains"   0 "$V1_COVERAGE_NOT_ARRAYS" "coverage arrays are missing or hold entries"
run_case "malformed selected entries abstain"      0 "$V1_SELECTED_STRINGS" "without a usable item_id/path"
run_case "an entry with no id or path abstains"    0 "$V1_ENTRY_NO_ID"      "without a usable item_id/path"
run_case "a malformed reused entry abstains"       0 "$V1_REUSED_STRING"    "without a usable item_id/path"
run_case "an empty id falls back to the path"      1 "$V1_EMPTY_ID"         "A partial review is not an approval"
run_case "  ...and passes when actually reviewed"  0 "$V1_EMPTY_ID_DONE"    "reviewed every item it selected"
run_case "no usable identity at all abstains"      0 "$V1_BOTH_EMPTY"       "without a usable item_id/path"
run_case "a path cannot alias another item_id"     1 "$V1_ID_COLLISION"     "covered 1 of 2"
run_case "  ...and names the real missing file"    1 "$V1_ID_COLLISION"     'OCR did not review `a.py`'
run_case "a newline in a path cannot merge items" 1 "$V1_NEWLINE_PATH"     "covered 2 of 3"
run_case "  ...and still names it readably"       1 "$V1_NEWLINE_PATH"     'a.py\nb.py'
run_case "a newline cannot forge two identities"  1 "$V1_NEWLINE_ALIAS"    "covered 2 of 3"
run_case "partial outranks a malformed coverage"  1 "$PARTIAL_BAD_COVERAGE" "A partial review is not an approval"
run_case "partial outranks an unknown schema"     1 "$PARTIAL_BAD_SCHEMA"  "A partial review is not an approval"
run_case "partial outranks an empty selection"    1 "$EMPTY_BUT_PARTIAL" "A partial review is not an approval"
run_case "  ...and admits it named nothing"       1 "$EMPTY_BUT_PARTIAL" "without naming which items"
run_case "a failed item outranks an empty set"    1 "$EMPTY_BUT_FAILED"  'OCR did not review `x.py`'

# A passing rerun must clear a stale "did not finish" rather than leave the PR
# claiming files were unreviewed after a run that covered them.
: > "$WORK/gh.log"; printf '%s' "$COMPLETE" > "$WORK/result.json"
PATH="$WORK/bin:$PATH" GH_TOKEN=x PR_NUMBER=1 REPO=o/r RUN_URL=http://run \
  OCR_FAKE_PRIOR_COMMENT=1 OCR_RESULT_FILE="$WORK/result.json" \
  OCR_COMMENT_FILE="$WORK/comment.md" GITHUB_STEP_SUMMARY="$WORK/summary.md" \
  bash --noprofile --norc -eo pipefail "$WORK/step.sh" >/dev/null 2>&1
if grep -q "PATCH" "$WORK/gh.log" && grep -q "OpenCodeReview completeness" "$WORK/comment.md" 2>/dev/null; then
  echo "  ok    a passing rerun clears a stale failure"; PASS=$((PASS+1))
else
  echo "  FAIL  a passing rerun left the stale failure comment"; FAIL=$((FAIL+1))
fi

# ...and with no prior comment it must not invent one.
: > "$WORK/gh.log"; printf '%s' "$COMPLETE" > "$WORK/result.json"
PATH="$WORK/bin:$PATH" GH_TOKEN=x PR_NUMBER=1 REPO=o/r RUN_URL=http://run \
  OCR_RESULT_FILE="$WORK/result.json" OCR_COMMENT_FILE="$WORK/comment.md" \
  GITHUB_STEP_SUMMARY="$WORK/summary.md" \
  bash --noprofile --norc -eo pipefail "$WORK/step.sh" >/dev/null 2>&1
if grep -q "PATCH" "$WORK/gh.log"; then
  echo "  FAIL  a clean pass edited a comment that does not exist"; FAIL=$((FAIL+1))
else
  echo "  ok    a clean pass writes no comment at all"; PASS=$((PASS+1))
fi

# The PR comment is a distinct channel from the annotations; check it moved.
: > "$WORK/gh.log"; printf '%s' "$PARTIAL" > "$WORK/result.json"
PATH="$WORK/bin:$PATH" GH_TOKEN=x PR_NUMBER=1 REPO=o/r RUN_URL=http://run \
  OCR_RESULT_FILE="$WORK/result.json" OCR_COMMENT_FILE="$WORK/comment.md" \
  GITHUB_STEP_SUMMARY="$WORK/summary.md" \
  bash --noprofile --norc -eo pipefail "$WORK/step.sh" >/dev/null 2>&1
if grep -q "pr comment" "$WORK/gh.log"; then
  echo "  ok    the PR is told, not just the log"; PASS=$((PASS+1))
else
  echo "  FAIL  no PR comment was attempted"; FAIL=$((FAIL+1))
fi

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
