"""Shared fixtures for the local_review.py tests (BEA-591).

The tests are split across test_detect_author.py, test_route.py and
test_verdict.py. One 540-line module was too large for OCR to review within
its per-file time limit (PR #73), and a review that times out is not a review.

Most of these are negative tests: each proves a rule REJECTS the input it
exists to reject. A routing check that only ever sees good input is the
"green while doing nothing" failure the blueprint's gates are built against.

Nothing here writes to the checkout or to $HOME: every file, including the
rotation state, lives in a per-test temp directory.

Run all:  python3 -m unittest discover -s scripts/local-review -v
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_review  # noqa: E402

SHA = "a" * 40
OLD_SHA = "b" * 40


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state", "rotation.json")
        self.n = 0

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = local_review.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def files(self, *paths):
        path = os.path.join(self.tmp, f"changed-{self.n}.txt")
        self.n += 1
        with open(path, "w") as fh:
            fh.write("\n".join(paths) + "\n")
        return path

    def result(self, reviewer, findings=(), commit=SHA, completed=True, model=None, raw=None):
        path = os.path.join(self.tmp, f"result-{self.n}.json")
        self.n += 1
        doc = {"schema": "local-review-result/v1", "reviewer": reviewer, "commit": commit,
               "completed": completed, "findings": list(findings)}
        if model is not None:
            doc["model"] = model
        with open(path, "w") as fh:
            fh.write(raw if raw is not None else json.dumps(doc))
        return path

    def route(self, author, *extra, files=("src/app.py",)):
        code, out, err = self.run_cli("route", "--author-family", author,
                                      "--changed-files", self.files(*files),
                                      "--state-file", self.state, *extra)
        return code, (json.loads(out) if code == 0 else None), err

    def verdict(self, author, results, files=("src/app.py",), commit=SHA):
        code, out, err = self.run_cli("verdict", "--author-family", author, "--commit", commit,
                                      "--changed-files", self.files(*files), *results)
        return code, (json.loads(out) if out.strip().startswith("{") else None), err


def finding(severity, title="something", **kw):
    return {"severity": severity, "title": title, **kw}
