"""Tests for author-family detection from commit trailers (local_review.py, BEA-591). Fixtures: lr_testkit.py."""

import json
import os
import unittest

from lr_testkit import Base


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

    def test_detect_author_rejects_a_fixer_as_reviewer_after_a_fix_round(self):
        # Codex wrote the branch; a Claude coordinator then committed fixes
        # without a trailer. Claude is now an author too and must not review.
        log = self.log("feat: x\n\nCo-Authored-By: Codex <noreply@openai.com>\n\nfix: y\n")
        code, out, _ = self.run_cli("detect-author", "--log-file", log, "--also", "anthropic")
        self.assertEqual(code, 0)
        authors = ",".join(json.loads(out)["families"])
        self.assertEqual(authors, "anthropic,openai")
        code, plan, _ = self.route(authors, "--no-advance")
        self.assertEqual({c["family"] for c in plan["candidates"]}, {"google"})

    def test_detect_author_rejects_also_as_a_stand_in_for_an_unknown_author(self):
        # No trailer names anyone: the real author is unknown, so adding the
        # coordinator must not turn that into "the coordinator wrote it".
        code, out, _ = self.run_cli("detect-author", "--log-file", self.log("fix: y\n"),
                                    "--also", "anthropic")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["families"], [])

    def test_first_round_keeps_the_coordinator_family_available(self):
        # Codex-written hold branch, before any fix: Claude and Google may both
        # review. Adding the coordinator early would leave one family.
        log = self.log("feat: x\n\nCo-Authored-By: Codex <noreply@openai.com>\n")
        code, out, _ = self.run_cli("detect-author", "--log-file", log)
        self.assertEqual(json.loads(out)["families"], ["openai"])
        code, plan, _ = self.route("openai", "--no-advance", files=(".github/workflows/ci.yml",))
        self.assertEqual(code, 0)
        self.assertEqual({c["family"] for c in plan["candidates"]}, {"anthropic", "google"})

    def test_an_empty_also_names_also_not_author_family(self):
        log = self.log("x\n\nCo-Authored-By: Codex <noreply@openai.com>\n")
        code, _, err = self.run_cli("detect-author", "--log-file", log, "--also", " ,")
        self.assertEqual(code, 2)
        self.assertIn("--also names no family", err)
        self.assertNotIn("--author-family", err)

    def test_detect_author_rejects_unknown_also_family(self):
        log = self.log("x\n\nCo-Authored-By: Codex <noreply@openai.com>\n")
        code, _, err = self.run_cli("detect-author", "--log-file", log, "--also", "meta")
        self.assertIn("unknown author family", err)
        self.assertEqual(code, 2)

    def test_detect_author_ignores_family_names_outside_trailers(self):
        code, _, _ = self.run_cli("detect-author", "--log-file", self.log(
            "chore: bump the claude and gemini clients\n"))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
