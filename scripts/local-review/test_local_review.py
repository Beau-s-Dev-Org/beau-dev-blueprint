"""Tests for local_review.py (BEA-591).

Most of these are negative tests: each proves a rule REJECTS the input it
exists to reject. A routing check that only ever sees good input is the
"green while doing nothing" failure the blueprint's gates are built against.

Nothing here writes to the checkout or to $HOME: every file, including the
rotation state, lives in a per-test temp directory.

Run:  python3 -m unittest discover -s scripts/local-review -v
"""

import contextlib
import io
import json
import os
import shutil
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


class DetectAuthor(Base):
    def log(self, text):
        path = os.path.join(self.tmp, "log.txt")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_detects_claude_trailer(self):
        code, out, _ = self.run_cli("detect-author", "--log-file", self.log(
            "feat: x\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["families"], ["anthropic"])

    def test_detects_every_family_on_a_mixed_branch(self):
        code, out, _ = self.run_cli("detect-author", "--log-file", self.log(
            "a\n\nCo-authored-by: Claude <noreply@anthropic.com>\n\n"
            "b\n\nCo-Authored-By: Codex <noreply@openai.com>\n"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["families"], ["anthropic", "openai"])

    def test_detect_author_rejects_branch_without_model_trailers(self):
        # A human co-author names no model family. Unknown must not route.
        code, out, _ = self.run_cli("detect-author", "--log-file", self.log(
            "fix: y\n\nCo-Authored-By: Pat Doe <pat@example.com>\n"))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["families"], [])

    def test_detect_author_ignores_family_names_outside_trailers(self):
        code, _, _ = self.run_cli("detect-author", "--log-file", self.log(
            "chore: bump the claude and gemini clients\n"))
        self.assertEqual(code, 2)


class Route(Base):
    def test_route_rejects_reviewers_from_the_author_family(self):
        for author, banned in (("anthropic", {"anthropic"}), ("openai", {"openai"}),
                               ("google", {"google"})):
            code, plan, _ = self.route(author, "--no-advance")
            self.assertEqual(code, 0, author)
            families = {c["family"] for c in plan["candidates"]}
            self.assertTrue(families, author)
            self.assertFalse(families & banned, f"{author} branch routed to its own family")

    def test_route_rejects_every_author_family_on_a_mixed_branch(self):
        code, plan, _ = self.route("anthropic,openai", "--no-advance")
        self.assertEqual(code, 0)
        self.assertEqual({c["family"] for c in plan["candidates"]}, {"google"})

    def test_route_rejects_unknown_author_family(self):
        code, _, err = self.route("mistral")
        self.assertEqual(code, 2)
        self.assertIn("unknown author family", err)

    def test_route_rejects_empty_author_family(self):
        code, _, _ = self.route(" , ")
        self.assertEqual(code, 2)

    def test_route_rejects_disabled_reviewers(self):
        code, plan, _ = self.route("anthropic", "--no-advance")
        self.assertNotIn("cursor-openai", [c["id"] for c in plan["candidates"]])

    def test_standard_path_needs_one_clean_family(self):
        code, plan, _ = self.route("anthropic", "--no-advance")
        self.assertEqual((plan["tier"], plan["required_clean_families"]), ("standard", 1))

    def test_hold_path_needs_two_clean_families(self):
        for path in (".github/workflows/ci.yml", "src/auth/login.py", "LICENSE",
                     "db/migrations/001.sql", "scripts/local-review/reviewers.yaml"):
            code, plan, _ = self.route("anthropic", "--no-advance", files=("README.md", path))
            self.assertEqual(code, 0, path)
            self.assertEqual(plan["tier"], "hold", path)
            self.assertEqual(plan["required_clean_families"], 2, path)
            self.assertIn(path, plan["hold_paths_matched"])

    def test_route_rejects_hold_tier_when_only_one_family_is_left(self):
        code, _, err = self.route("anthropic", "--exclude", "antigravity",
                                  files=(".github/workflows/ci.yml",))
        self.assertEqual(code, 3)
        self.assertIn("needs 2", err)

    def test_route_rejects_when_no_reviewer_is_left(self):
        code, _, _ = self.route("anthropic", "--exclude", "codex,antigravity")
        self.assertEqual(code, 3)

    def test_exclude_drops_a_failed_reviewer(self):
        code, plan, _ = self.route("anthropic", "--no-advance", "--exclude", "codex")
        self.assertEqual([c["id"] for c in plan["candidates"]], ["antigravity"])

    def test_rotation_cycles_through_eligible_reviewers(self):
        firsts = [self.route("anthropic")[1]["candidates"][0]["id"] for _ in range(3)]
        self.assertEqual(firsts, ["codex", "antigravity", "codex"])

    def test_no_advance_leaves_rotation_alone(self):
        self.route("anthropic", "--no-advance")
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.route("anthropic")[1]["candidates"][0]["id"], "codex")

    def test_corrupt_rotation_state_falls_back_to_the_start(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.route("anthropic")[0], 0)


class Verdict(Base):
    def test_clean_when_one_other_family_finds_no_p1_p2(self):
        code, v, _ = self.verdict("anthropic", [self.result("codex", [finding("P3")])])
        self.assertEqual(code, 0)
        self.assertTrue(v["clean"])
        self.assertEqual(v["clean_families"], ["openai"])

    def test_verdict_rejects_p1_or_p2_findings(self):
        for sev in ("P1", "P2"):
            code, v, _ = self.verdict("anthropic", [self.result("codex", [finding(sev)])])
            self.assertEqual(code, 1, sev)
            self.assertEqual(len(v["blocking"]), 1, sev)

    def test_verdict_rejects_a_p1_even_from_an_ineligible_reviewer(self):
        # A same-family reviewer cannot make a branch clean, but a bug it found
        # is still a bug.
        code, v, _ = self.verdict("anthropic", [self.result("codex"),
                                                self.result("claude", [finding("P1")])])
        self.assertEqual(code, 1)
        self.assertEqual(v["blocking"][0]["reviewer"], "claude")

    def test_verdict_rejects_a_clean_review_from_the_author_family(self):
        code, v, _ = self.verdict("openai", [self.result("codex")])
        self.assertEqual(code, 1)
        self.assertEqual(v["clean_families"], [])
        self.assertIn("author's family", v["ineligible"][0]["reason"])

    def test_verdict_rejects_reviews_of_an_older_commit(self):
        code, v, _ = self.verdict("anthropic", [self.result("codex", commit=OLD_SHA)])
        self.assertEqual(code, 1)
        self.assertEqual(len(v["stale"]), 1)

    def test_stale_findings_do_not_block_the_current_commit(self):
        code, _, _ = self.verdict("anthropic", [self.result("codex", [finding("P1")], commit=OLD_SHA),
                                                self.result("codex")])
        self.assertEqual(code, 0)

    def test_verdict_rejects_an_unreadable_result_even_beside_a_clean_one(self):
        for raw in ("{not json", "[]", json.dumps({"schema": "other/v1"})):
            code, v, _ = self.verdict("anthropic", [self.result("codex"),
                                                    self.result("antigravity", raw=raw)])
            self.assertEqual(code, 1, raw)
            self.assertEqual(len(v["invalid"]), 1, raw)

    def test_verdict_rejects_unknown_severity(self):
        # "critical" is not P1; a result that cannot be graded is not clean.
        code, v, _ = self.verdict("anthropic", [self.result("codex", [finding("critical")])])
        self.assertEqual(code, 1)
        self.assertIn("severity", v["invalid"][0]["reason"])

    def test_verdict_rejects_findings_that_are_not_a_list(self):
        path = self.result("codex", raw=json.dumps({"schema": "local-review-result/v1",
                                                    "reviewer": "codex", "commit": SHA,
                                                    "completed": True, "findings": "none"}))
        self.assertEqual(self.verdict("anthropic", [path])[0], 1)

    def test_verdict_rejects_an_incomplete_review(self):
        for completed in (False, "true", None):
            code, _, _ = self.verdict("anthropic", [self.result("codex", completed=completed)])
            self.assertEqual(code, 1, completed)

    def test_verdict_rejects_unknown_and_disabled_reviewers(self):
        for reviewer in ("someone", "cursor-openai"):
            code, v, _ = self.verdict("anthropic", [self.result(reviewer)])
            self.assertEqual(code, 1, reviewer)
            self.assertEqual(len(v["ineligible"]), 1, reviewer)

    def test_verdict_rejects_multi_provider_review_on_the_wrong_family(self):
        # Antigravity can run Claude. If it did, it reviewed a Claude branch
        # with Claude, whatever the roster says its family is.
        for model in (None, "claude-sonnet", ""):
            code, _, _ = self.verdict("anthropic", [self.result("antigravity", model=model)])
            self.assertEqual(code, 1, model)
        code, _, _ = self.verdict("anthropic", [self.result("antigravity", model="gemini-pro")])
        self.assertEqual(code, 0)

    def test_hold_tier_rejects_a_single_clean_family(self):
        hold = (".github/workflows/ci.yml",)
        code, _, _ = self.verdict("anthropic", [self.result("codex")], files=hold)
        self.assertEqual(code, 1)
        # Two reviews from ONE family are still one family.
        code, _, _ = self.verdict("anthropic", [self.result("codex"), self.result("codex")], files=hold)
        self.assertEqual(code, 1)
        code, v, _ = self.verdict("anthropic", [self.result("codex"),
                                                self.result("antigravity", model="gemini-pro")],
                                  files=hold)
        self.assertEqual(code, 0)
        self.assertEqual(v["clean_families"], ["google", "openai"])

    def test_verdict_rejects_no_results(self):
        self.assertEqual(self.verdict("anthropic", [])[0], 1)

    def test_verdict_rejects_an_abbreviated_commit(self):
        code, _, err = self.verdict("anthropic", [self.result("codex")], commit=SHA[:12])
        self.assertEqual(code, 2)
        self.assertIn("40-character", err)

    def test_markdown_section_reports_the_result_and_escapes_cells(self):
        code, out, _ = self.run_cli("verdict", "--author-family", "anthropic", "--commit", SHA,
                                    "--changed-files", self.files("src/app.py"),
                                    "--markdown", "--rounds", "3",
                                    self.result("codex", [finding("P2", title="a | b\nc")]))
        self.assertEqual(code, 1)
        self.assertIn("**Result: not clean**", out)
        self.assertIn("Rounds: 3", out)
        self.assertIn("a \\| b c", out)

    def test_markdown_section_for_a_clean_branch(self):
        code, out, _ = self.run_cli("verdict", "--author-family", "anthropic", "--commit", SHA,
                                    "--changed-files", self.files("src/app.py"), "--markdown",
                                    self.result("codex"))
        self.assertEqual(code, 0)
        self.assertIn("**Result: clean.**", out)
        self.assertIn("OCR runs next as the final gate.", out)


class Config(Base):
    def config(self, text):
        path = os.path.join(self.tmp, "reviewers.yaml")
        with open(path, "w") as fh:
            fh.write(text)
        return path

    BASE = """schema: local-review-config/v1
families: {anthropic: 'claude', openai: 'gpt', google: 'gemini'}
hold_paths: []
reviewers:
"""

    def load(self, reviewers):
        code, _, err = self.run_cli("--config", self.config(self.BASE + reviewers),
                                    "route", "--author-family", "anthropic",
                                    "--changed-files", self.files("x"), "--state-file", self.state)
        return code, err

    def test_shipped_config_loads(self):
        local_review.load_config(local_review.DEFAULT_CONFIG)

    def test_config_rejects_a_multi_provider_model_from_another_family(self):
        code, err = self.load("  - {id: c, family: google, agent: cursor, multi_provider: true,"
                              " model: claude-sonnet, enabled: true}\n")
        self.assertEqual(code, 2)
        self.assertIn("does not match", err)

    def test_config_rejects_duplicate_ids(self):
        code, err = self.load("  - {id: c, family: openai, agent: codex, enabled: true}\n"
                              "  - {id: c, family: google, agent: antigravity, enabled: true}\n")
        self.assertEqual(code, 2)
        self.assertIn("duplicate", err)

    def test_config_rejects_unknown_family(self):
        code, err = self.load("  - {id: c, family: meta, agent: x, enabled: true}\n")
        self.assertEqual(code, 2)
        self.assertIn("unknown family", err)

    def test_config_rejects_wrong_schema(self):
        code, _, _ = self.run_cli("--config", self.config("schema: v0\n"), "unavailable",
                                  "--reason", "x")
        self.assertEqual(code, 2)

    def test_enabled_must_be_literally_true(self):
        # `enabled: "false"` is a truthy string; it must not switch a reviewer on.
        code, _ = self.load("  - {id: c, family: openai, agent: codex, enabled: 'false'}\n")
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
