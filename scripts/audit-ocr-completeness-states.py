#!/usr/bin/env python3
"""Enumerate every input state the OCR completeness gate can face, and print
what it decides for each.

Not a pass/fail test — scripts/test-ocr-completeness.sh is that. This is the
state audit CLAUDE.md's repeated-patch rule requires: six consecutive commits
had reworked that gate for input shapes found one at a time by reviewers, which
is the point at which the rule says to stop patching and enumerate.

Run it after any change to the gate and read the table. Every row should be
obviously right; a surprising row is the finding. Running it the first time is
what turned up the unset-GITHUB_STEP_SUMMARY crash, which no review had raised.

  python3 scripts/audit-ocr-completeness-states.py
"""

import json, os, subprocess, sys, tempfile, pathlib, shutil
import yaml

# Extract the step under test from the workflow, exactly as the test script
# does. The first version read a fixed /tmp/step.sh that some earlier command
# in the author's shell happened to have created — on a clean checkout every
# case ran a nonexistent file, reported "?? rc=127", and the script still
# exited 0. An audit for silent failure that fails silently is not an audit.
ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ocr-review.yml"
STEP_NAME = "Fail if the review only partially ran"

WORK = pathlib.Path(tempfile.mkdtemp())
STEP = WORK / "step.sh"

def _extract():
    doc = yaml.safe_load(WORKFLOW.read_text())
    steps = [x for j in doc["jobs"].values() for x in j.get("steps", [])]
    match = [x for x in steps if x.get("name") == STEP_NAME]
    if len(match) != 1:
        sys.exit(f"expected exactly one step named {STEP_NAME!r}, found {len(match)}")
    STEP.write_text("#!/bin/bash\n" + match[0]["run"])

_extract()
(WORK / "bin").mkdir()
(WORK / "bin" / "gh").write_text('#!/bin/sh\nexit 0\n')
(WORK / "bin" / "gh").chmod(0o755)

ENV = {**os.environ, "PATH": f"{WORK}/bin:" + os.environ["PATH"], "GH_TOKEN": "x",
       "PR_NUMBER": "1", "REPO": "o/r", "RUN_URL": "http://r",
       "OCR_RESULT_FILE": str(WORK / "result.json"),
       "OCR_COMMENT_FILE": str(WORK / "comment.md"),
       "GITHUB_STEP_SUMMARY": str(WORK / "summary.md")}


def run(doc, env=None, raw=None):
    (WORK / "summary.md").write_text("")
    (WORK / "result.json").write_text(raw if raw is not None else json.dumps(doc))
    # The same wrapper flags Actions applies to a `shell: bash` step.
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(STEP)],
                       capture_output=True, text=True, env={**ENV, **(env or {})})
    out = r.stdout + r.stderr + (WORK / "summary.md").read_text()
    if r.returncode == 1: verdict = "FAIL (incomplete)"
    elif "could not be verified" in out: verdict = "abstain (warn)"
    elif "selected no files" in out: verdict = "pass (nothing selected)"
    elif "reviewed every item" in out: verdict = "pass (complete)"
    else: verdict = f"?? rc={r.returncode}"
    return verdict, out

def cov(sel=(), done=(), failed=(), reused=(), waived=(), status="success", term="completed", schema="ocr.run-manifest/v1"):
    e = lambda xs: [x if isinstance(x, dict) else {"path": x} for x in xs]
    m = {"schema_version": schema, "terminal_state": term,
         "coverage": {"selected": e(sel), "completed": e(done), "failed": e(failed),
                      "reused": e(reused), "waived": e(waived)}}
    return {"status": status, "manifest": m}

BARE_STRINGS = json.dumps({"status": "success", "manifest": {
    "schema_version": "ocr.run-manifest/v1", "terminal_state": "completed",
    "coverage": {"selected": ["a.py"], "completed": [], "failed": [],
                 "reused": [], "waived": []}}})

CASES = [
 # --- the result document itself ---
 ("file missing/empty",                    dict(raw="")),
 ("not valid JSON",                        dict(raw="{nope")),
 ("valid JSON, no manifest",               dict(doc={"status": "success"})),
 # --- schema ---
 ("schema absent",                         dict(doc={"status":"success","manifest":{"coverage":{"selected":[],"completed":[],"failed":[]}}})),
 ("schema v2",                             dict(doc=cov(schema="ocr.run-manifest/v2"))),
 # --- coverage container ---
 ("coverage absent",                       dict(doc={"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1"}})),
 ("coverage is a list",                    dict(doc={"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","coverage":[]}})),
 ("selected not an array",                 dict(doc={"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","coverage":{"selected":"a","completed":[],"failed":[]}}})),
 ("reused/waived absent (optional)",       dict(doc={"status":"success","manifest":{"schema_version":"ocr.run-manifest/v1","terminal_state":"completed","coverage":{"selected":[{"path":"a"}],"completed":[{"path":"a"}],"failed":[]}}})),
 # --- entries ---
 ("entry is a bare string",                dict(raw=BARE_STRINGS)),
 ("entry with neither id nor path",        dict(doc=cov(sel=[{"fingerprint":"x"}]))),
 ("entry item_id empty, path usable",      dict(doc=cov(sel=[{"item_id":"","path":"a"}], done=[]))),
 ("entry item_id numeric, path usable",    dict(doc=cov(sel=[{"item_id":7,"path":"a"}], done=[{"item_id":7,"path":"a"}]))),
 # --- arithmetic ---
 ("nothing selected, nothing else",        dict(doc=cov())),
 ("nothing selected but a failed entry",   dict(doc=cov(failed=[{"path":"x","classification":"timeout"}]))),
 ("all selected completed",                dict(doc=cov(sel=["a","b"], done=["a","b"]))),
 ("some completed",                        dict(doc=cov(sel=["a","b"], done=["a"]))),
 ("none completed",                        dict(doc=cov(sel=["a","b"]))),
 ("reused + waived cover the rest",        dict(doc=cov(sel=["a","b","c"], done=["a"], reused=["b"], waived=["c"]))),
 ("covered has an item never selected",    dict(doc=cov(sel=["a"], done=["a","z"]))),
 ("selected lists the same item twice",    dict(doc=cov(sel=["a","a"], done=["a"]))),
 ("failed item also appears completed",    dict(doc=cov(sel=["a"], done=["a"], failed=[{"path":"a","classification":"timeout"}]))),
 # --- partial signals against every abstain path ---
 ("unreadable (partial unknowable)",       dict(raw="")),
 ("bad JSON (partial unknowable)",         dict(raw='{"status":"partial"')),
 ("partial + schema v2",                   dict(doc=cov(schema="ocr.run-manifest/v2", status="partial"))),
 ("partial + coverage absent",             dict(doc={"status":"partial","manifest":{"schema_version":"ocr.run-manifest/v1"}})),
 ("partial + everything otherwise fine",   dict(doc=cov(sel=["a"], done=["a"], status="partial"))),
 ("terminal partial, status fine",         dict(doc=cov(sel=["a"], done=["a"], term="partial"))),
 # --- identity aliasing ---
 ("path aliases another item_id",          dict(doc=cov(sel=[{"item_id":"a.py","path":"first"},{"item_id":"","path":"a.py"}], done=[{"item_id":"a.py","path":"first"}]))),
 ("newline path",                          dict(doc=cov(sel=["a","b","a\nb"], done=["a","b"]))),
 ("newline forging tagged ids",            dict(doc=cov(sel=["a","b","a\npath:b"], done=["a","b"]))),
 # --- environment ---
 ("no PR number (push event)",             dict(doc=cov(sel=["a","b"], done=["a"]), env={"PR_NUMBER": ""})),
 ("gh pr comment fails",                   dict(doc=cov(sel=["a","b"], done=["a"]), env={"PATH": "/nonexistent:" + os.environ["PATH"]})),
]
print(f"  {'case':42} {'verdict'}")
print("  " + "-" * 70)
unclassified = []
for name, kw in CASES:
    v, _ = run(kw.get("doc"), kw.get("env"), kw.get("raw"))
    print(f"  {name:42} {v}")
    if v.startswith("??"):
        unclassified.append(name)
shutil.rmtree(WORK, ignore_errors=True)
if unclassified:
    print()
    print(f"  {len(unclassified)} case(s) produced no recognisable verdict — the audit "
          "did not run against the gate:")
    for n in unclassified:
        print(f"    - {n}")
    sys.exit(1)
print(f"\n  {len(CASES)} states enumerated; every one produced a recognisable verdict.")
