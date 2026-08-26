# beau-dev-blueprint — AI Agent Instructions

## What this repo is

Blueprint holds the **shared review and automation machinery every other repo depends on**:

- `.github/workflows/ocr-review.yml` — the reusable OCR PR-review workflow. Consuming repos pin it by commit SHA and call it from a thin stub. **Review logic is edited here, not in the consumers.**
- `.agents/scripts/review_pr.py` — this repo's own automated PR reviewer.
- `.agents/scripts/decompose.py` — the Conductor proposal-decomposition step.
- `.agents/scripts/_shared.py` — the LLM provider chain and response parsing both scripts use.
- `CLAUDE-template.md` — a template for *consuming* projects. Not this repo's instructions; this file is.

Consuming repos: `beauzone/marketing-as-code`, `mac-engine`, `mac-cli`, `mac-client`, `mac-contract`, `mac-mcp-server`, `mac-registry`.

## Merge policy

Branch + PR for every change. **Never commit directly to `main`.**

Self-merge is authorised only when **all** of the following hold:

1. CI is green on the final commit.
2. The automated review pipeline completed, every review thread is resolved, and no P1/P2 finding is unresolved.
3. No commits were pushed after the final review approval.

**A review that did not run is not approval.** A green check is not sufficient evidence on its own — read the review output. Two known ways a check reports green with no review behind it:

- The circuit breaker returns exit 0 when the cycle cap is reached (BEA-444).
- OCR reports `Review skipped: no items were selected` for a diff it cannot select, e.g. Markdown-only (BEA-446).

**Never merge over a red review check.** PRs #33 and #34 were both merged while `review` was failing on a retired model, which is how a dead reviewer went unnoticed for over a month (BEA-428). A red review check means the gate did not run — treat it as blocking, not as noise.

### Hold-for-Beau — do not self-merge; leave the PR open and notify

The standing categories are: auth/RBAC/identity, governance enforcement paths, security scanning and the quarantine pipeline, GitHub Actions / workflow / CI configuration, secrets or credential handling, LICENSE changes, database schema or migrations, customer-facing copy, **and any change to the review or merge pipeline itself**.

Note what that last category means here: **almost everything in this repo is the review or merge pipeline.** Changes to `.agents/scripts/**` and `.github/workflows/**` are hold-for-Beau by default. Self-merge in blueprint should be rare and deliberate — a README typo, not a workflow edit.

A change here propagates to every consuming repo that pins it, so the blast radius of a bad merge is the whole fleet.

## Review rounds

`MAX_REVIEW_CYCLES` is 25, not a small number, and that is deliberate. Real round counts on substantial PRs in the consuming repos: 24 (marketing-as-code PR #224), 23 (BEA-374), 13 (BEA-413), 9 (BEA-304). A low cap does not prevent runaway cost so much as truncate legitimate review — and because the breaker returns success, a truncated review presents as a passing check.

**Repeated-patch discipline.** If the same function or region gets a fix in **two consecutive** review rounds, stop taking the next narrow fix: enumerate every state and input shape that code must handle, then make one change covering all of them. Nine of PR #224's 24 rounds re-patched the same ~30 lines, and a defect introduced by one of those patches survived an extra full round. `review_pr.py` applies this to itself — past `REPETITION_WARNING_AFTER_CYCLES` the reviewer is instructed to enumerate rather than narrow-fix.

## LLM provider configuration

**Never name a model in code.** A pinned model name is what broke this pipeline for a month (BEA-428). The primary provider is a router (`openrouter/auto`), which has no version to retire, and every model name, endpoint, and key is workflow config — so a deprecation is a settings change, not a pull request.

The chain is ordered tiers, each a `(LLM_URL*, LLM_API_KEY*, <PURPOSE>_MODEL*)` triple. A tier missing any of the three is skipped and announced. Advancing to the next tier covers 402 credits, 401/403 auth, 404 unknown model, 410 retired, 429, 5xx, unreachable endpoints, and unreadable responses. **Exhausting every tier must raise, never return empty** — a reviewer that could not run must never present as a passing check.

If you replace or add a model, verify it responds with a **real call**. Presence in a model list is not proof: `glm-4.7:cloud` and `qwen3-coder:480b-cloud` both appeared current and were retired.

## GitHub Actions conventions

**No untrusted context value inline in a `run:` or `script:` body.** A `${{ }}` expression is substituted textually before the shell or JS parses the line, so a value containing a quote, backtick, or `$(...)` executes as code on the runner. Route every such value — `steps.*.outputs.*`, `inputs.*`, event fields — through a step's `env:` block and reference it as `"$VAR"` (bash) or `process.env.VAR` (JS). This is the BEA-327 class; `conductor.yml` carried two instances of it.

## Working conventions

- Verify claims against the actual state, not against prior documents or a model list.
- When a fix is reported, say what proves it — a live call, a test run, a re-read of the file. "Should work" is not a result.
- Conventional commits. Reference the driving issue (`Refs BEA-XX`).
