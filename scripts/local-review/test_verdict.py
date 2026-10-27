"""Tests for the clean verdict, its PR-body section, and input handling (local_review.py, BEA-591). Fixtures: lr_testkit.py."""

import json
import os
import unittest

from lr_testkit import Base, OLD_SHA, SHA, finding


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

    def test_verdict_rejects_a_malformed_commit_id_as_unreadable_not_stale(self):
        # Every shape a commit field can take that is not exactly a full SHA.
        for bad in (None, "", 123, [SHA], {"sha": SHA}, SHA[:12], SHA + "a", "z" * 40,
                    SHA + "\n", "\n" + SHA, " " + SHA, SHA + " ", SHA[:20] + " " + SHA[20:39],
                    "\uff41" * 40):
            path = self.result("codex", [finding("P1")], commit=bad)
            code, v, _ = self.verdict("anthropic", [self.result("codex"), path])
            self.assertEqual(code, 1, bad)
            self.assertEqual(len(v["invalid"]), 1, bad)
            self.assertEqual(v["stale"], [], bad)

    def test_an_uppercase_sha_is_the_same_commit(self):
        self.assertEqual(self.verdict("anthropic", [self.result("codex", commit=SHA.upper())])[0], 0)

    def test_verdict_rejects_a_malformed_reviewer_field(self):
        for bad in (None, "", 7, ["codex"], {"id": "codex"}):
            path = self.result("codex", [finding("P1")])
            with open(path) as fh:
                doc = json.load(fh)
            doc["reviewer"] = bad
            with open(path, "w") as fh:
                json.dump(doc, fh)
            code, v, _ = self.verdict("anthropic", [self.result("codex"), path])
            self.assertEqual(code, 1, bad)
            self.assertEqual(len(v["invalid"]), 1, bad)

    def test_verdict_rejects_a_head_commit_argument_with_a_trailing_newline(self):
        self.assertEqual(self.verdict("anthropic", [self.result("codex")], commit=SHA + "\n")[0], 2)

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

    def test_verdict_rejects_one_family_for_a_skill_only_change(self):
        # The review skill is review pipeline: one family cannot approve it.
        code, v, _ = self.verdict("anthropic", [self.result("codex")],
                                  files=(".skills/local-review/SKILL.md",))
        self.assertEqual(code, 1)
        self.assertEqual(v["tier"], "hold")

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
        # An eligible review that found a P2 does not count toward clean.
        self.assertIn("| codex | openai | no | 0 | 1 | 0 |", out)

    def test_markdown_explains_a_not_clean_result_with_no_findings(self):
        code, out, _ = self.run_cli("verdict", "--author-family", "anthropic", "--commit", SHA,
                                    "--changed-files", self.files(".github/workflows/ci.yml"),
                                    "--markdown", self.result("codex"),
                                    self.result("claude"), self.result("codex", commit=OLD_SHA))
        self.assertEqual(code, 1)
        self.assertIn("Needs 2 non-author families clean; 1 came back clean (openai).", out)
        self.assertIn("Did not count", out)
        self.assertIn("author's family", out)
        self.assertIn("a review of a different commit", out)

    def test_a_non_ascii_utf8_result_is_read_as_utf8(self):
        # ensure_ascii=False, so real multi-byte UTF-8 reaches the file, and a
        # P1 so the decoded title comes back in `blocking`: read as latin-1 or
        # the locale encoding, it would come back garbled.
        title = "caf\u00e9 \u2014 \u2713"
        raw = json.dumps({"schema": "local-review-result/v1", "reviewer": "codex", "commit": SHA,
                          "completed": True, "findings": [finding("P1", title=title)]},
                         ensure_ascii=False)
        code, v, _ = self.verdict("anthropic", [self.result("codex", raw=raw)])
        self.assertEqual(code, 1)
        self.assertEqual(v["invalid"], [])
        self.assertEqual(v["blocking"][0]["title"], title)

    def test_crlf_changed_files_still_match_hold_paths(self):
        path = os.path.join(self.tmp, "crlf.txt")
        with open(path, "w", newline="") as fh:
            fh.write("README.md\r\n.github/workflows/ci.yml\r\n")
        code, out, _ = self.run_cli("verdict", "--author-family", "anthropic", "--commit", SHA,
                                    "--changed-files", path, self.result("codex"))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["tier"], "hold")

    def test_clean_headline_counts_clean_families_not_a_ratio(self):
        code, out, _ = self.run_cli("verdict", "--author-family", "anthropic", "--commit", SHA,
                                    "--changed-files", self.files("src/app.py"), "--markdown",
                                    self.result("codex"),
                                    self.result("antigravity", model="gemini-pro"))
        self.assertEqual(code, 0)
        self.assertIn("2 non-author families (google, openai) found no P1/P2", out)
        self.assertIn("(1 required)", out)
        self.assertNotIn("2 of 1", out)

    def test_markdown_escapes_line_and_keeps_line_zero(self):
        for line, expect in ((0, "(a.py:0)"), ("3|4\n5", "(a.py:3\\|4 5)")):
            code, out, _ = self.run_cli("verdict", "--author-family", "anthropic", "--commit", SHA,
                                        "--changed-files", self.files("src/app.py"), "--markdown",
                                        self.result("codex", [finding("P2", file="a.py", line=line)]))
            self.assertEqual(code, 1)
            self.assertIn(expect, out)

    def test_markdown_section_for_a_clean_branch(self):
        code, out, _ = self.run_cli("verdict", "--author-family", "anthropic", "--commit", SHA,
                                    "--changed-files", self.files("src/app.py"), "--markdown",
                                    self.result("codex"))
        self.assertEqual(code, 0)
        self.assertIn("**Result: clean.**", out)
        self.assertIn("OCR runs next as the final gate.", out)


class Inputs(Base):
    def test_unreadable_inputs_exit_2_not_1(self):
        # Exit 1 means "not clean"; a typo in a path must not read as a verdict.
        missing = os.path.join(self.tmp, "nope.txt")
        for argv in (("verdict", "--author-family", "anthropic", "--commit", SHA,
                      "--changed-files", missing),
                     ("route", "--author-family", "anthropic", "--changed-files", missing,
                      "--state-file", self.state),
                     ("detect-author", "--log-file", missing)):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 2, argv[0])
            self.assertIn("cannot read", err)

    def test_non_utf8_inputs_exit_2_not_1(self):
        bad = os.path.join(self.tmp, "latin1.txt")
        with open(bad, "wb") as fh:
            fh.write(b"src/caf\xe9.py\n")
        for argv in (("verdict", "--author-family", "anthropic", "--commit", SHA,
                      "--changed-files", bad),
                     ("detect-author", "--log-file", bad)):
            code, _, err = self.run_cli(*argv)
            self.assertEqual(code, 2, argv[0])
            self.assertIn("cannot read", err)
        with open(os.path.join(self.tmp, ".local-review-hold-paths"), "wb") as fh:
            fh.write(b"^caf\xe9/\n")
        code, _, err = self.run_cli("route", "--author-family", "anthropic", "--repo-root", self.tmp,
                                    "--changed-files", self.files("x"), "--state-file", self.state)
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_rotation_saves_to_a_bare_filename(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        code, _, err = self.run_cli("route", "--author-family", "anthropic",
                                    "--changed-files", self.files("x"), "--state-file", "rot.json")
        self.assertEqual(code, 0)
        self.assertNotIn("warning", err)
        with open(os.path.join(self.tmp, "rot.json")) as fh:
            self.assertEqual(json.load(fh), {"pointer": 1})
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith("tmp")], [])


if __name__ == "__main__":
    unittest.main()
