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
}
env = match[0].get("env") or {}
# This suite runs the body under bash; the workflow must too.
if match[0].get("shell") != "bash":
    sys.exit(f"the limits step's shell is {match[0].get('shell')!r}, not 'bash'")
for k, v in want.items():
    if env.get(k) != v:
        sys.exit(f"env {k} is {env.get(k)!r}, expected {v!r}")
# Exactly these inputs: the deadline no longer depends on PR size (#73).
if set(env) != set(want):
    sys.exit(f"the limits step's env is {sorted(env)}, expected {sorted(want)}")
# By id, so an attempt that LOST these inputs fails too, not only one with
# wrong values.
attempts = [s for s in steps if s.get("id") in ("ocr-primary", "ocr-fallback1", "ocr-fallback2")]
# The attempts resolve steps.limits.outputs, which the with: checks below cannot
# see: a renamed id or a step moved below them leaves all three inputs empty.
if match[0].get("id") != "limits":
    sys.exit(f"the limits step's id is {match[0].get('id')!r}, not 'limits'")
# Per job: outputs do not cross jobs, so all four must share one job, with
# limits ahead of every attempt, not just the first.
attempt_ids = ("ocr-primary", "ocr-fallback1", "ocr-fallback2")
homes = [[s.get("id") for s in j.get("steps", [])] for j in doc["jobs"].values()
         if any(s.get("id") == "limits" for s in j.get("steps", []))]
if len(homes) != 1 or not all(a in homes[0] for a in attempt_ids):
    sys.exit("the limits step and all three review attempts are not in one job")
# The job timeout must cover every step budget, or an attempt is cancelled
# mid-review instead of failing inside a step that can report it.
job = next(j for j in doc["jobs"].values()
           if any(s.get("id") == "limits" for s in j.get("steps", [])))
budgets = [s.get("timeout-minutes") for s in job.get("steps", [])]
if any(not isinstance(b, int) for b in budgets):
    sys.exit("a step in the review job has no timeout-minutes, so its reserve is unenforceable")
if not isinstance(job.get("timeout-minutes"), int) or job["timeout-minutes"] < sum(budgets):
    sys.exit(f"job timeout-minutes {job.get('timeout-minutes')!r} is below its step budgets' sum {sum(budgets)}")
for a in attempt_ids:
    if homes[0].index("limits") > homes[0].index(a):
        sys.exit(f"the limits step does not run before {a}")
if len(attempts) != 3:
    sys.exit(f"expected 3 review attempt steps, found {len(attempts)}")
# The error output only helps if production posts it: a dropped or renamed
# consumer would make a limits failure silent again.
report = [s for s in steps if s.get("name") == "Report a limits error on the PR"]
if (len(report) != 1
        or (report[0].get("env") or {}).get("LIMITS_ERROR") != "${{ steps.limits.outputs.error }}"):
    sys.exit("the limits step's error output is no longer posted to the PR")
for s in attempts:
    # Gated explicitly, so a failed limits step cannot run an attempt with
    # empty inputs; and 60 minutes each, the budget the body's 50-minute
    # split and the expectations below all assume.
    # Leading clause and no ||: a substring check would accept
    # "... == 'success' || always()", which bypasses the guard.
    cond = " ".join(str(s.get("if", "")).replace("${{", "").replace("}}", "").split())
    if not cond.startswith("steps.limits.outcome == 'success'") or "||" in cond:
        sys.exit(f"attempt {s.get('name')!r} is not gated on the limits step succeeding")
    # 80: the grouping request (at most one 25-minute deadline) plus two
    # rounds of 25, with room for install and posting. The step body's
    # 25-minute cap is what this assumes.
    if s.get("timeout-minutes") != 80:
        sys.exit(f"attempt {s.get('name')!r} has timeout-minutes {s.get('timeout-minutes')!r}, not 80")
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
# $1 expected exit, $2 llm seconds, $3 task minutes, $4 label,
# $5 text the output must contain, $6 expected "task llm" outputs,
# $7 text the output must NOT contain.
# A rejection must show ITS message: an unrelated exit 1 (an unset variable,
# a typo in a test) would otherwise satisfy every rejection case.
check() {
  : > "$WORK/gh_out"
  LLM_TIMEOUT_SEC="$2" TASK_TIMEOUT_MIN="$3" GITHUB_OUTPUT="$WORK/gh_out" \
    bash --noprofile --norc -eo pipefail "$WORK/step.sh" >"$WORK/out" 2>&1   # as Actions runs it
  local got=$? why=""
  [ "$got" -eq "$1" ] || why="expected exit $1, got $got"
  [ -z "$why" ] && [ -n "${5:-}" ] && ! grep -qF -- "$5" "$WORK/out" && why="output lacks: $5"
  [ -z "$why" ] && [ -n "${7:-}" ] && grep -qF -- "$7" "$WORK/out" && why="output contains: $7"
  if [ -z "$why" ] && [ -n "${6:-}" ]; then
    local t l
    t=$(sed -n 's/^task_minutes=//p' "$WORK/gh_out"); l=$(sed -n 's/^llm_timeout_sec=//p' "$WORK/gh_out")
    [ "$t $l" = "$6" ] || why="outputs '$t $l', expected '$6'"
  fi
  if [ -z "$why" ]; then pass=$((pass+1)); echo "  ok    $4"
  else fail=$((fail+1)); echo "  FAIL  $4 ($why)"; sed 's/^/        /' "$WORK/out"; fi
}

read -r DEF_LLM DEF_TASK < "$WORK/defaults"
check 0 "$DEF_LLM" "$DEF_TASK" "shipped defaults pass ($DEF_LLM s, $DEF_TASK min)" "" "$DEF_TASK $DEF_LLM"
# Annotations land on every consuming PR, so their ABSENCE at the defaults is
# pinned too: an unconditional one would pass every case below.
check 0 1200 25 "the defaults raise no warning" "" "" "::warning::"
check 0 1200 25 "the defaults emit no notice"   "" "" "::notice::"

# The deadline is the configured value capped at 25, whatever the PR's size:
# OCR bundles files into groups, and the group count is not known in advance.
check 0 1200 25 "the configured deadline is used as is"        "80 min per attempt" "25 1200"
check 0 600  10 "a lower configured deadline is kept"          "" "10 600"
check 0 300  25 "a request below the deadline is kept"         "" "25 300"
check 0 1501 25 "a request longer than its deadline is clamped, with a warning" "it is clamped to that deadline" "25 1500"
check 0 1200 26 "a deadline over 25 is clamped, with a warning" "using at most 25" "25 1200"
check 0 3000 30 "an older stub with higher settings still gets a review" "::warning::" "25 1500"
check 0 1200 10 "a request over a lower deadline is clamped to it" "" "10 600"
check 1 01200 25 "rejects a zero-padded request timeout"       "LLM_TIMEOUT_SEC must be a positive integer"
check 1 1200 025 "rejects a zero-padded deadline"              "TASK_TIMEOUT_MIN must be a positive integer"

# Whether the request limit was clamped, for the notice's advice.
flags() {  # $1 llm, $2 task, $3 label, $4 expected llm_shortened
  : > "$WORK/gh_out"
  LLM_TIMEOUT_SEC="$1" TASK_TIMEOUT_MIN="$2" GITHUB_OUTPUT="$WORK/gh_out" \
    bash --noprofile --norc -eo pipefail "$WORK/step.sh" >"$WORK/out" 2>&1
  local rc=$? got
  # Flags written before a later failure would pass here while production
  # skipped every attempt.
  if [ "$rc" -ne 0 ]; then
    fail=$((fail+1)); echo "  FAIL  $3 (step exited $rc)"; sed 's/^/        /' "$WORK/out"; return
  fi
  got=$(sed -n 's/^llm_shortened=//p' "$WORK/gh_out")
  if [ "$got" = "$4" ]; then pass=$((pass+1)); echo "  ok    $3"
  else fail=$((fail+1)); echo "  FAIL  $3 (llm_shortened '$got', expected '$4')"; fi
}
flags 1200 25 "a request inside the deadline is not shortened" false
flags 1500 25 "a request exactly at the deadline is not shortened" false
flags 1200 10 "a request over the deadline is shortened"       true

: > "$WORK/gh_out"
LLM_TIMEOUT_SEC=1200 TASK_TIMEOUT_MIN=25 GITHUB_OUTPUT="$WORK/gh_out" \
  bash --noprofile --norc -eo pipefail "$WORK/step.sh" >"$WORK/out" 2>&1
rc=$?
if [ "$rc" -eq 0 ] && grep -qx "effort=medium" "$WORK/gh_out"; then
  pass=$((pass+1)); echo "  ok    effort is pinned to medium"
else
  fail=$((fail+1)); echo "  FAIL  effort output is not 'medium'"; sed 's/^/        /' "$WORK/out"
fi
# The removed size-derived outputs must stay gone: a consumer still reading
# one would get an empty value.
if ! grep -qE '^(task_shortened|task_ceiling)=' "$WORK/gh_out"; then
  pass=$((pass+1)); echo "  ok    no size-derived outputs"
else
  fail=$((fail+1)); echo "  FAIL  a size-derived output is still written"; sed 's/^/        /' "$WORK/gh_out"
fi

# A failure also writes its reason as an output, newline-free.
: > "$WORK/gh_out"
LLM_TIMEOUT_SEC=$'12\ninjected=1' TASK_TIMEOUT_MIN=25 GITHUB_OUTPUT="$WORK/gh_out" \
  bash --noprofile --norc -eo pipefail "$WORK/step.sh" >"$WORK/out" 2>&1
rc=$?
# Must also FAIL: an error output on a green step would run every attempt
# with empty inputs.
if [ "$rc" -ne 0 ] && [ "$(grep -c '^error=' "$WORK/gh_out")" = 1 ] && ! grep -q '^injected=' "$WORK/gh_out"; then
  pass=$((pass+1)); echo "  ok    a failure's reason is one output line, newlines stripped"
else
  fail=$((fail+1)); echo "  FAIL  error output missing or injectable"; sed 's/^/        /' "$WORK/gh_out"
fi
check 1 1200 0   "rejects a zero deadline"                     "TASK_TIMEOUT_MIN must be a positive integer"
check 1 0    25  "rejects a zero request timeout"              "LLM_TIMEOUT_SEC must be a positive integer"
check 1 ""   25  "rejects an empty request timeout"            "LLM_TIMEOUT_SEC must be a positive integer"
check 1 1200 ""  "rejects an empty deadline"                   "TASK_TIMEOUT_MIN must be a positive integer"
check 1 "12a" 25 "rejects a non-numeric request timeout"       "LLM_TIMEOUT_SEC must be a positive integer"
check 1 1200 "2.5" "rejects a fractional deadline"             "TASK_TIMEOUT_MIN must be a positive integer"
check 1 "-5" 25  "rejects a negative request timeout"          "LLM_TIMEOUT_SEC must be a positive integer"
# Anchored on the literal value: an executed substitution would drop the $( or
# backticks, so this fails whatever the surrounding punctuation becomes. The
# bare-sentinel check stays as a second line of defence.
check 1 '$(echo INJECTED_SENTINEL_9f3)' 25 "treats a command substitution as data" \
  "must be a positive integer, got '\$(echo INJECTED_SENTINEL_9f3)'" "" "INJECTED_SENTINEL_9f3'."
check 1 '`echo INJECTED_SENTINEL_9f3`' 25 "treats backticks as data" \
  "must be a positive integer, got '\`echo INJECTED_SENTINEL_9f3\`'" "" "INJECTED_SENTINEL_9f3'."

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
