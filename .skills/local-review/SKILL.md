---
name: local-review
description: Run cross-family PR review rounds locally through Orca before a pull request exists, then open the PR for OCR's final review once the branch is clean. Use when a branch is ready for review, before running `gh pr create`, or when asked to "review locally", "run local review", or "get this ready for a PR".
---

# Local review (BEA-591)

Most review rounds happen here, on Beau's Mac, on paid subscriptions. The
GitHub OCR review is the **final gate**, and should usually pass first time
because the branch arrives clean.

The routing and the "is it clean?" decision are made by
`scripts/local-review/local_review.py` in the blueprint, not by you. Do not
override its answer, re-grade a finding, or count a review it rejected.

## Rules

- **No PR during local rounds.** Pushing the branch for backup is fine: OCR runs
  only on PR events. Open the PR only once the verdict is clean, or when local
  review cannot run (the last section).
- **A reviewer never comes from the author's model family.** `route` enforces it.
- **Clean** means no P1/P2 on the exact head commit, from one non-author family
  (two, on hold-for-Beau paths). `verdict` decides it.
- **Reviewers only review.** They do not edit, commit, or push. Fixes are yours as
  the branch author. This skill assigns them to you, which overrides Orca's
  default that a coordinator does not edit after a review-only `worker_done`.
- **Stop and escalate**, rather than starting another round, when `max_rounds`
  is reached, or when the same function or region needs a fix in two consecutive
  rounds (the repeated-patch rule in the blueprint's CLAUDE.md). Escalate with the
  open findings, the options, and a recommendation.

## Prerequisites

Both found by running this skill for real (2026-10-26):

- **The repo is registered in Orca** (`orca repo list --json`). If it isn't, Orca
  cannot start agents in it: `worker-start` and `agent launch` fail with
  `selector_not_found`. Registering it changes Beau's Orca setup, so ask him
  first, then run `orca repo add`.
- **The coordinator runs inside an Orca terminal.** Orchestration commands
  issued from anywhere else (the Claude desktop app, a plain shell) fail with
  `no_active_sender_terminal`. If you are not in one, do not coordinate from
  where you are. Launch a coordinator into the branch's worktree instead, and
  wait for its report:

  ```text
  orca agent launch --agent claude --worktree path:<worktree> --surface terminal --prompt "<follow this skill for branch X; author family Y; report to <RESULTS>/coordinator-report.md>" --json
  ```

  The coordinator's own family does not matter. It routes and fixes, it does
  not review.

## Setup, once per session

```bash
BP="${BLUEPRINT_HOME:-$HOME/dev/beau-dev-blueprint}"
LR="python3 $BP/scripts/local-review/local_review.py"
BASE=origin/main            # the PR's base branch
git fetch origin
RESULTS="$(git rev-parse --absolute-git-dir)/local-review/$(git rev-parse --abbrev-ref HEAD)"
mkdir -p "$RESULTS"
```

`$RESULTS` sits inside `.git`, so nothing written there can be committed.
The script needs PyYAML (`python3 -m pip install pyyaml`).

Load Orca's version-matched orchestration guide and follow it for every Orca
command below. It is the source of truth for exact verbs and flags:

```bash
orca skills get orchestration
```

## 1. Who wrote it

```bash
$LR detect-author --base "$BASE"
```

Exit 2 means no `Co-Authored-By:` trailer names a model family. Do not guess.
Pass your own family explicitly: you know which model you are.

## 2. Each round

Commit everything first. A review is of a commit, not a working tree.

```bash
HEAD_SHA=$(git rev-parse HEAD)
ROUND_DIR="$RESULTS/round-$N"; mkdir -p "$ROUND_DIR"
$LR route --author-family "$AUTHOR" --base "$BASE"
```

`route` prints `tier`, `required_clean_families`, `max_rounds`, and an ordered
`candidates` list. Exit 3 means the enabled reviewers cannot satisfy the rules:
go to the last section.

Create one Orca Run for the whole review (`orchestration run-create`, objective
"Local review of <branch>"). For each family you still need this round, take
the first candidate whose family is not yet covered, and start it as a
supervised worker in this worktree:

```text
orca orchestration worker-start --worktree current --agent <candidate.agent> [--model <candidate.model>] --task-title "Review <branch> @ <short sha>" --spec "<reviewer spec below>" --json
```

Pass `--model` only when the candidate's `model` is non-empty. Start every
reviewer you need for the round before waiting, so they run in parallel.

### Reviewer spec

Fill in the placeholders. Keep the rest word for word:

> **Target:** the changes on this branch, `git diff <BASE>...<HEAD_SHA>`, in the
> current worktree. Read the surrounding code as needed.
>
> **Change:** none. This is a review. Do not edit, create, or delete any file in
> the repository, and do not commit, push, or open a PR.
>
> **Find:** correctness bugs, security issues, data loss, broken contracts or
> callers, missing negative tests for a gate or validator, and anything that
> breaks a rule in the repo's CLAUDE.md or AGENTS.md. Severity: **P1** breaks
> users, data, or security, or blocks the release. **P2** is a real defect that
> must be fixed before merge. **P3** is a nice-to-have. Do not inflate the
> severity of style points, and do not report a finding you have not checked
> against the code.
>
> **Output:** write exactly one JSON file to `<ROUND_DIR>/<candidate.id>.json`:
> `{"schema": "local-review-result/v1", "reviewer": "<candidate.id>", "commit": "<HEAD_SHA>", "model": "<the model you are running on>", "completed": true, "findings": [{"severity": "P1|P2|P3", "file": "path", "line": 0, "title": "one line", "detail": "why it is wrong and how to fix it"}]}`.
> Use `"findings": []` if there are none. Set `"completed": false` if you could
> not review all of the diff.
>
> **Acceptance:** the file exists and is valid JSON, and `git status --porcelain`
> is unchanged from when you started. Then send `worker_done` with
> `--outcome succeeded`. If you hit a rate limit or cannot finish, send
> `--outcome failed` and say why.

### Waiting, and reviewers that fail

Wait for the round with `orchestration check --wait`, as Orca's guide describes.
When a reviewer's Dispatch fails, or Orca shows positive proof its agent exited,
treat it as unavailable for this round: re-run `route` with
`--exclude <comma-separated failed ids>` and `--no-advance`, and start the next
candidate for the family still needed. Release every settled worker. Never
start a duplicate while a reviewer is still live or `unverifiable`.

After the round, confirm the reviewers left the tree alone:
`git status --porcelain` must match what it was before, and `git rev-parse HEAD`
must still be `$HEAD_SHA`. If either changed, that round does not count:
discard the changes, and start again from step 2 with a different reviewer.

## 3. Verdict

```bash
$LR verdict --author-family "$AUTHOR" --commit "$HEAD_SHA" --base "$BASE" "$ROUND_DIR"/*.json
```

- **Exit 0, clean:** go to step 4.
- **Exit 1, not clean:** fix each P1/P2 in `blocking` and each item in
  `invalid`, commit, and start the next round (`N+1`) from step 2. P3s are
  optional: fix them, or list them in the PR body. A file listed under `invalid`
  or `ineligible` means a review did not count. Read the reason, don't skip it.

## 4. Open the PR

```bash
$LR verdict --author-family "$AUTHOR" --commit "$HEAD_SHA" --base "$BASE" --markdown --rounds "$N" "$ROUND_DIR"/*.json > "$RESULTS/section.md"
git push -u origin HEAD
```

Paste `section.md` into the **Local review** section of the PR body, and add one
line per earlier round on what it found and what you fixed. Then open the PR.

Check the remote head matches before you report it as done:
`git ls-remote origin HEAD` against `git rev-parse HEAD`. OCR then reviews it.
If OCR finds a P1/P2, fix it, then run a local round on the new commit before
you push again. OCR is not where the rounds happen.

## When local review cannot run

If `route` exits 3, or every candidate fails in a round, do not wait for a
subscription to come back:

```bash
$LR unavailable --reason "<what happened, e.g. codex and antigravity both rate-limited>" > "$RESULTS/section.md"
```

Push, open the PR with that section, and let OCR run the review rounds the way
it did before local review existed. Tell Beau in one line that local review was
skipped, and why.
