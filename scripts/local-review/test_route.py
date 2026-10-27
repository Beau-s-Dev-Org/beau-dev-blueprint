"""Tests for routing, hold-for-Beau tiers, and config validation (local_review.py, BEA-591). Fixtures: lr_testkit.py."""

import json
import os
import subprocess
import unittest

from lr_testkit import Base, local_review


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
        ids = [c["id"] for c in plan["candidates"]]
        self.assertEqual(ids, ["codex", "antigravity"])   # exactly the enabled non-author ones
        self.assertNotIn("cursor-openai", ids)

    def test_standard_path_needs_one_clean_family(self):
        code, plan, _ = self.route("anthropic", "--no-advance")
        self.assertEqual((plan["tier"], plan["required_clean_families"]), ("standard", 1))

    def test_hold_path_needs_two_clean_families(self):
        for path in (".github/workflows/ci.yml", "src/auth/login.py", "LICENSE",
                     "db/migrations/001.sql", "scripts/local-review/reviewers.yaml",
                     ".skills/local-review/SKILL.md", "templates/ocr-review-caller.yml",
                     "CLAUDE.md", "CLAUDE-template.md", "pkg/AGENTS.md",
                     "db/schema.sql", "prisma/schema.prisma", "alembic/versions/0001_init.py",
                     "queries/report.sql", "schemas/decision-record.json"):
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
        code, plan, _ = self.route("anthropic")
        self.assertEqual(code, 0)
        self.assertEqual(plan["candidates"][0]["id"], "codex")

    def test_an_unsatisfiable_route_does_not_consume_a_rotation_slot(self):
        self.assertEqual(self.route("anthropic", "--exclude", "codex,antigravity")[0], 3)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.route("anthropic")[1]["candidates"][0]["id"], "codex")


class RepoHoldPaths(Base):
    """The per-repo hold list, and the real git path inventory."""

    def tier(self, root, *paths):
        code, out, err = self.run_cli("route", "--author-family", "anthropic", "--no-advance",
                                      "--changed-files", self.files(*paths),
                                      "--repo-root", root, "--state-file", self.state)
        return code, (json.loads(out)["tier"] if code == 0 else err)

    def hold_file(self, text):
        with open(os.path.join(self.tmp, ".local-review-hold-paths"), "w") as fh:
            fh.write(text)

    def test_repo_file_puts_its_paths_on_hold(self):
        self.assertEqual(self.tier(self.tmp, "scripts/build.sh"), (0, "standard"))
        self.hold_file("# comment\n\n^scripts/\n")
        self.assertEqual(self.tier(self.tmp, "scripts/build.sh"), (0, "hold"))

    def test_blueprint_holds_its_ocr_gate_scripts(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for path in ("scripts/test-ocr-completeness.sh", "scripts/audit-ocr-completeness-states.py",
                     "scripts/rollout-ocr.sh", "scripts/requirements.txt"):
            self.assertEqual(self.tier(root, path), (0, "hold"), path)

    def test_repo_hold_file_rejects_a_bad_pattern(self):
        self.hold_file("^scripts/(\n")
        code, err = self.tier(self.tmp, "x")
        self.assertEqual(code, 2)
        self.assertIn("bad pattern", err)

    def test_an_unreadable_repo_hold_file_is_an_error_not_absent(self):
        # Present but unreadable must not quietly drop the repo's hold paths.
        os.mkdir(os.path.join(self.tmp, ".local-review-hold-paths"))
        code, err = self.tier(self.tmp, "copy/home.md")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_a_hold_file_behind_a_permission_error_is_an_error(self):
        root = os.path.join(self.tmp, "locked")
        os.mkdir(root)
        with open(os.path.join(root, ".local-review-hold-paths"), "w") as fh:
            fh.write("^copy/\n")
        os.chmod(root, 0o600)   # readable listing, no search: stat fails
        self.addCleanup(os.chmod, root, 0o700)
        code, err = self.tier(root, "copy/home.md")
        self.assertEqual(code, 2)

    def test_editing_the_repo_hold_file_is_itself_on_hold(self):
        self.assertEqual(self.tier(self.tmp, ".local-review-hold-paths"), (0, "hold"))

    def test_route_rejects_a_rename_out_of_a_hold_path_as_standard(self):
        # Real git: rename detection would report only archive/ci.yml.
        repo = os.path.join(self.tmp, "repo")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

        def git(*a):
            subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, env=env)

        os.makedirs(os.path.join(repo, ".github", "workflows"))
        with open(os.path.join(repo, ".github", "workflows", "ci.yml"), "w") as fh:
            fh.write("name: ci\non: push\njobs: {}\n")
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-qm", "init")
        git("checkout", "-qb", "feature")
        os.makedirs(os.path.join(repo, "archive"))
        git("mv", ".github/workflows/ci.yml", "archive/ci.yml")
        git("commit", "-qm", "move")
        cwd = os.getcwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, cwd)
        code, out, _ = self.run_cli("route", "--author-family", "anthropic", "--base", "main",
                                    "--no-advance", "--state-file", self.state)
        self.assertEqual(code, 0)
        plan = json.loads(out)
        self.assertEqual(plan["tier"], "hold")
        self.assertIn(".github/workflows/ci.yml", plan["hold_paths_matched"])


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

    def test_config_rejects_hold_paths_that_are_not_a_list_of_strings(self):
        for hold in ("'^scripts/'", "{'^a': 1}", "[1]"):
            path = self.config(self.BASE.replace("hold_paths: []", f"hold_paths: {hold}")
                               + "  - {id: c, family: openai, agent: codex, enabled: true}\n")
            code, _, err = self.run_cli("--config", path, "unavailable", "--reason", "x")
            self.assertEqual(code, 2, hold)
            self.assertIn("hold_paths must be a list", err)

    def test_config_rejects_unknown_family(self):
        code, err = self.load("  - {id: c, family: meta, agent: x, enabled: true}\n")
        self.assertEqual(code, 2)
        self.assertIn("unknown family", err)

    def test_config_rejects_non_utf8_bytes_with_exit_2(self):
        path = os.path.join(self.tmp, "reviewers.yaml")
        with open(path, "wb") as fh:
            fh.write(self.BASE.encode() + b"  - {id: caf\xe9, family: openai, agent: codex, enabled: true}\n")
        code, _, err = self.run_cli("--config", path, "unavailable", "--reason", "x")
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

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
