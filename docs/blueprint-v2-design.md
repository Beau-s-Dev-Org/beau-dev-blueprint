# beau-dev-blueprint v2 — Design

Status: Approved by Beau 2026-09-06; delivered as the sequence of PRs in §11 on `Beau-s-Dev-Org/beau-dev-blueprint` (all hold-for-Beau, since every one touches CI or the review pipeline). The new Marketing as Code monorepo is the first repo stamped from it. Tracking: BEA-508 (PR 1) and successors.

## 1. What the blueprint becomes

Today the repo is one working component (the reusable OCR review workflow that every MaC repo calls by SHA), one stale toolchain (Antigravity/LiteLLM/Ollama profiles, the Conductor, model names from early 2026 in five files), and one docs protocol (PROP-NNN proposals promoted to D-numbered ADRs) that predates the way MaC actually records decisions.

v2 turns it into two things and nothing else:

1. **A GitHub template repo.** `gh repo create <name> --template Beau-s-Dev-Org/beau-dev-blueprint` produces a project that already has the directory skeleton, the agent rules, the decision and spec system, the CI lanes, CODEOWNERS with the hold-for-human paths, the evals workspace, and the Linear conventions. Day one of a new project is writing the first spec, not assembling scaffolding.
2. **A library of reusable workflows and scripts** that stamped repos call by commit SHA. The OCR review workflow stays exactly as it is; the gates below join it. `onboard.sh` shrinks to a re-stamp tool that bumps the pinned SHA in a consumer and opens the PR.

Everything in the repo is either part of the template, part of the reusable library, or deleted.

## 2a. Who this is for, and the division of labour

This blueprint is built for a **non-engineer builder**: a person who owns the product, the decisions, and the outcomes, but does not read code for correctness and should not be asked to. Every process in it is designed around that fact rather than around a conventional engineering team, and the design goal is stated plainly: **remove the human from every step where they do not contribute, and make the steps where they do contribute cheap, clear, and decision-shaped.**

What the agents own, with no human in the loop:

- Code review. Correctness, security, style, and regressions are found by multi-model LLM review run in rounds until the PR is clean, with the number of rounds and models scaling with the risk tier of the paths touched. A human never reads a diff to decide whether it is safe; the gates decide that.
- Merging, for anything not on the hold list, once the `merge-policy` check confirms the conditions.
- Testing, including writing the negative tests, and keeping the proof path green.
- Sequencing and running multi-PR builds, including overnight, using the rules in AGENTS.md and `HANDOFF.md` between runs.
- Keeping derived surfaces, docs, counts, and versions in sync, because those are build steps, not tasks.

What the human owns, and is brought in for, nothing else:

1. **Decisions.** Anything that needs a decision record: architecture that constrains how things must be built, security and governance boundaries, public or product claims, scope changes through a proposal. The agent brings a one-line ask, two or three options with upside, downside, and risk, and a recommendation. The human decides; the agent records it.
2. **Consequential, hard-to-reverse actions.** Production deploys, data migrations, secret rotation, licensing, customer-facing copy, and changes to the review and merge pipeline itself. These are the CODEOWNERS paths. The human does not review the diff; the agent presents a **decision brief** (what changes, why, what could go wrong, how it is verified, how it is rolled back) and the human approves the action.
3. **Product acceptance.** Does the thing do what was intended, for the user it was intended for. This is the skill a builder actually has, and the blueprint reserves their time for it: exit tests are phrased so the human can run or watch them, and the reference example and smoke tests exist so acceptance is a demonstration, not a code read.
4. **Exceptions.** When the agents cannot converge (review rounds exhausted, a gate that cannot be satisfied without a decision, a contradiction between specs), they stop and escalate with the same options-and-recommendation shape. They do not guess.

Consequences for the rest of this document: the hold-for-Beau list is a list of *decisions the human owns*, not code the human reviews; every hold-for-Beau PR ships with a decision brief in its description; the review gate is risk-tiered (more rounds and a second model family on CODEOWNERS paths); and AGENTS.md states this division of labour explicitly so every agent, on every surface, works from the same understanding of when to proceed and when to stop and ask.

## 2. Principles, each with the failure it exists to prevent

These are the lessons, stated as rules the template enforces. The failure column is real: every one happened in MaC v0.

| Rule | Failure it prevents |
|---|---|
| **Spec before build.** A module is not buildable until it has a spec with a contract, a gate, and an exit criterion phrased as a runnable test. | 268 build cycles with architecture asserted after the fact; 14 of 20 "decided" items with no record. |
| **Contract first, then parallel.** Dependents start when the layer below freezes its contract, not when it is implemented. Two gates per layer: *contract frozen* unlocks dependents, *exit test green* unlocks release. | Clients and skills built against an unpublished, moving server. |
| **Derive, don't author.** Any surface that restates another (help text, catalogs, detection lists, mirrors, SDK stubs) is generated from the source of truth in a build step, never hand-written. | Five plugin skills frozen for six weeks while the server merged 100 PRs; three copies of each skill, one current. |
| **Every gate ships with a negative test.** A check that only ever sees good input is decoration. Merging a gate requires a test proving it fails on bad input. | Five governance checks returning green while doing nothing (validate, score, blast radius, drift, MDR schema). |
| **Constraints are machine-checkable or they don't exist.** Prose in a schema description, a comment, or a README is not a constraint on AI-authored artifacts. | Five independent authors invented plausible enum values because the enum was in a description. |
| **Done means consumers updated.** An issue that changes an interface cannot close until every surface that depends on it passes conformance. | BEA-130 closed with the manifest still saying "populated once BEA-130 ships." |
| **One layer open at a time above the core.** The vision is fixed; the sequence is revised at gates; scope never changes mid-layer. New capabilities enter through proposal → decision → spec, never straight to a build. | Seven repos, two desktop shells, three workspace rewires, and a marketplace, all open at once, for one founder. |
| **Markdown is code.** Skills, prompts, help, and specs are product surfaces and get the same review, versioning, and gates as code. | Markdown-only PRs passed the review gate unreviewed (BEA-446). |
| **Tests don't touch the tree.** No test writes into the checkout or the home directory; test tooling lives in its own workspace with its own lockfile. | Test runs mutating tracked files and the developer's `~/.claude` (BEA-472, 477, 480). |
| **Pin, don't float.** Actions, reusable workflows, and dependencies are pinned by SHA or exact version; a floating pin is a CI failure. | Prod outage from an unpinned fastmcp (BEA-199); a review agent dead for six weeks on a retired model (BEA-428). |
| **Never name a model in code.** Model selection is configuration with a fallback chain. | Retired model names in five blueprint files. |
| **Report outcomes, not process.** Every agent status update and final report leads with the bottom line. | (Beau's standing rule; encoded in AGENTS.md so it doesn't have to be pasted into every prompt.) |

## 3. The template skeleton

What a stamped repo contains. Language-agnostic; Python and TypeScript get first-class defaults, anything else gets the structure and empty lanes.

```
/
├── AGENTS.md                  canonical agent rules (Claude Code, Codex, Cursor all read it)
├── CLAUDE.md                  three lines: "read AGENTS.md"; project-specific overrides only
├── CODEOWNERS                 hold-for-human paths → @Beau
├── README.md                  what this repo is, how to run the proof path, nothing that drifts
├── LICENSE / LICENSES/ / REUSE.toml   path-based license tiers (see §8)
├── HANDOFF.md                 agent-to-agent branch handoff, with an "honest gaps" section
├── .github/
│   ├── workflows/
│   │   ├── ci.yml             one workflow, path-classified lanes (docs / contract / full)
│   │   ├── review.yml         stub calling blueprint ocr-review.yml@<sha>
│   │   ├── gates.yml          stub calling blueprint gates.yml@<sha> (see §5)
│   │   └── release.yml        stub calling blueprint release.yml@<sha>
│   ├── ISSUE_TEMPLATE/        spec, proposal, defect — each with the Definition-of-Done checklist
│   ├── PULL_REQUEST_TEMPLATE.md   "proof path run", "consumers updated", "negative test added"
│   └── dependabot.yml
├── docs/
│   ├── vision.md              the fixed thing; changes require a decision record
│   ├── roadmap.md             layers, each with contract / gate / exit test / status; re-sequenced at gates only
│   ├── specs/                 one file per module: contract, gates, exit test, status header
│   ├── decisions/             DR-NNNN.yaml, schema-validated (MaC's MDR schema, generalized)
│   ├── proposals/             PROP-NNNN: the door new scope enters through
│   ├── evaluations/           external comparisons (the OpenWork/OpenDesign pattern) with a conversion obligation
│   ├── open-questions.md      dated; a question older than 90 days fails the docs lane
│   └── runbooks/              deploy, emergency change, incident
├── contract/                  the published interface: OpenAPI / tool schemas / events / errors — generated, never edited
├── src/  or  packages/        product code; pure-domain packages expose ports + a conformance test kit
├── clients/                   anything that may import only contract/ (enforced by the boundary linter)
├── evals/                     separate workspace + lockfile; the only proof path
│   ├── specs/                 tests, one directory per docs/specs entry
│   ├── testkit/
│   └── fixtures/
├── scripts/
│   ├── check_boundaries       import-boundary linter config (clients → contract only; specs → testkit only)
│   ├── check_decisions        schema-validate DRs, resolve every cross-reference, ratchet the baseline
│   ├── check_stale_refs       "until BEA-xxx ships" comments where the issue is Done
│   ├── check_derived          every derived surface equals its regeneration
│   └── check_pins             no floating action/workflow/dependency pins
└── .skills/                   process skills the agent runs: write-a-spec, prove-a-pr, handoff, release
```

Dropped from v1: `.agents/`, `profiles/`, `proposals/` at root, `CONDUCTOR_BUILD_PLAN.md`, `config.yaml`, `Brewfile`, `setup-quality.sh`, `.vscode/`, `test.txt`, `.DS_Store`.

The blueprint repo itself is the skeleton above plus the library it serves: `.github/workflows/{ocr-review,gates,release}.yml` as `workflow_call` workflows, `templates/` with the caller stubs, `scripts/stamp.sh` (today's `rollout-ocr.sh`, extended to pin all three stubs), `schemas/` for DRs and specs, and `examples/hello-contract/` (§11, PR 6). The blueprint runs its own gates on itself, so the template is never stale relative to the library.

## 4. Rules: AGENTS.md and CODEOWNERS

**AGENTS.md is canonical and short.** Claude Code, Codex, and Cursor all read it; `CLAUDE.md` points to it and carries only project-specific overrides. Contents, in order: what the repo is (three sentences), the proof path ("the only proof is a passing test under `evals/specs/`; prose, screenshots, and 'should work' never decide pass/fail"), the communication rules block Beau currently pastes into every build prompt (verbatim, so it stops being pasted), the branch and merge policy, the self-merge conditions, the hold-for-human list as a pointer to CODEOWNERS, the decision rule ("if it constrains how something must be built, is a security or governance boundary, is a public claim, or rejects an option someone will re-propose, it needs a DR"), the Linear status discipline, and the overnight-build rules (separate repos in parallel, same repo sequenced, don't queue behind a hold-for-Beau PR, orchestrator for complex builds, name the model tier per task).

**CODEOWNERS makes hold-for-Beau mechanical.** The list Beau already uses, as paths: `.github/**`, `contract/**`, anything matching `auth|rbac|identity|security|quarantine|secrets|license|migrations`, customer-facing copy directories declared per project, and `scripts/check_*`. Branch protection requires a code-owner review on those paths, so an agent physically cannot self-merge them, which is the backstop the policy has lacked.

## 5. The gates (reusable `gates.yml`)

One reusable workflow with named jobs; a stamped repo enables the ones that apply. Each is a script in `scripts/` so it runs locally too. Every gate is merged with its own negative test in the blueprint's `evals/`.

| Gate | What it checks | Origin |
|---|---|---|
| `review` | The existing OCR workflow, unchanged. Add: a markdown-only PR is still reviewed (BEA-446), and a review that did not run reports red, not green (BEA-444). | v1, hardened |
| `decisions` | DR schema validation with enums enforced structurally; every `related`/`supersedes` reference resolves to a file; prose under "Decision" headings in docs cites a DR id; violation count ratchets down from a baseline. | BEA-461, BEA-466 |
| `derived` | Regenerate every derived surface (contract from code, catalogs from contract, mirrors from source) and fail on diff. | Drift findings; OpenWork's OpenAPI→catalog derivation |
| `conformance` | Every consumer surface (skills, prompts, help, clients, SDK stubs) references only names present in `contract/`; the contract's consumer test kit passes against the implementation. | The gate that would have caught the critique |
| `boundaries` | Import-boundary linter: `clients/` may import only `contract/`; `evals/specs` only the testkit; pure-domain packages import no infrastructure. | OpenWork dependency-cruiser |
| `negative` | Any PR touching a file under `scripts/check_*`, a validator, a scanner, or a scorer must add or change a test whose name matches `test_*_rejects_*`. | "Green while doing nothing" |
| `stale-refs` | Comments or docs referencing a Linear issue as future ("until BEA-x", "once BEA-x ships") where the issue is Done. | BEA-130 |
| `docs` | Open questions older than 90 days; spec status headers valid; roadmap layer statuses consistent with gate results; counts in docs are computed, never typed. | MaC docs-conformance, generalized |
| `pins` | No `@main`/`@v3` action refs, no unpinned reusable workflows, no floating dependency ranges in lockfile-less ecosystems. | BEA-199, BEA-428 |
| `hygiene` | No `${{ }}` inside `run:`/`script:` (BEA-327 class); explicit `secrets:` on reusable calls, never `inherit`; tests wrote nothing to the tree (`git status --porcelain` empty after the run). | v1 rule, BEA-472/477 |
| `security` | Dependabot on; CodeQL or language equivalent; path-scoped LLM security review that fires only on cross-boundary changes (contract + server + client in one PR). | OpenWork warden pattern |
| `agent-smoke` | The scripted happy path over the public surface, surface-only credentials (§7a). Runs in the contract lane and nightly. | The Kymata Marketer run |
| `merge-policy` | Evaluates the self-merge conditions from the GitHub API (CI green on final commit, review completed with threads resolved and no open P1/P2, no commits after approval) and is itself a required status; CODEOWNERS paths always require the human. | MaC self-merge policy, made checkable |

`ci.yml` in the stamped repo classifies the change (docs-only, contract-touching, full) and runs the matching lane, so a docs PR takes two minutes and a contract PR runs everything. The 48-workflow sprawl in MaC v0 becomes four files.

## 6. Decisions, specs, proposals

**Decision records** generalize MaC's MDR: `docs/decisions/DR-NNNN-slug.yaml`, schema in the blueprint, enums structural, `supersedes` and `superseded_by` both present, a `review_trail` section (the OpenWork note pattern, so the lenses that reviewed the decision are recorded), and an id-allocation README so the 0011–0076 gap never recurs. Projects with a domain-specific record type (MaC's MDR) extend the schema; they do not replace it.

**Specs** are one file per module with a mandatory status header (`Draft | Contract frozen | Building | Exit test green | Released`), and four required sections: contract (what dependents may rely on, with a pointer into `contract/`), gates (which `gates.yml` jobs apply), exit test (a path under `evals/specs/`), and decisions (DR ids this spec depends on). The `docs` gate refuses a spec that lacks any of the four. The `.skills/write-a-spec` skill produces this shape so an agent can't produce a different one.

**Proposals** are the door. `PROP-NNNN` → review with the `[V]/[T]/[P]` labeling from the v1 collaborator guide (the one part of that protocol worth keeping) → a DR if accepted → a spec → Linear issues. An evaluation under `docs/evaluations/` that reaches "implement" must convert into a proposal within a set window or be closed (BEA-463's conversion obligation, generalized).

## 7. Testing and evals

`evals/` is its own workspace with its own lockfile so test tooling never leaks into the product build. Rules: one test directory per spec, named identically; negative tests mandatory for every gate and validator; fixtures are checked in and versioned; no test touches the tree or `$HOME`; flaky tests are quarantined by a nightly report, not tolerated. The test count is computed in CI and written nowhere by hand.

## 7a. Agent-surface tests (the bot as a test harness)

Any project with a surface that AI agents consume (MCP tools, served skills, help, an API meant for agent clients) gets a third kind of test alongside unit and contract tests: an agent exercising the surface the way a real user-agent would. The MaC critique proved this is the only test that finds "connected but unusable" defects; the blueprint makes it routine. Three tiers, each with a home under `evals/agent/`:

Only the smoke tier is a CI gate, and only because it is scripted, fast, and needs no LLM. The bot-driven tiers are **opt-in per project, never a merge gate, and run at milestones**, not on every PR: the bot may be unavailable, it is slow, and most PRs do not change what an agent experiences. `project.yaml` declares whether the harness is enabled at all and at which milestones it runs.

| Tier | What it is | When it runs | Pass/fail |
|---|---|---|---|
| **Smoke** | Scripted, deterministic: the happy path over the public surface only (for MaC: manifest → overview → voice → messaging → template → score → submit → log). No LLM in the loop. | Every PR in the contract lane; nightly against staging. The one tier that gates. | Any 404, unknown tool name, leaked internal URL or path, or non-conforming response shape. |
| **Scenario** | An LLM agent given a persona and job-shaped tasks ("find the approved proof points for the CIO audience and draft a LinkedIn post"), run against staging with only the public surface available (no checkout, no local pack). Outcomes are scored by a rubric: found the right data, used the right tool, refused what should be refused, produced the artifact through the sanctioned path. | At the end of an epic, before a release, or when a PR is labelled `agent-test`. Invoked by a person or the `plan-issues` skill when it closes an epic; never automatic per PR. | Rubric thresholds per scenario. A failure files an issue against the epic; it does not block the merge that triggered it. |
| **Critique** | The unbiased-reviewer run: a fresh agent, no roadmap access, reads every tool description and skill, uses the product for real tasks, and writes the report. Output is diffed against the previous run. | At layer gates and, optionally, on a schedule the project sets. | Not pass/fail. The delta becomes proposals; unaddressed findings older than one cycle are listed in the next report, not gated. |

`project.yaml`:

```yaml
agent_tests:
  enabled: false          # default; true for projects with an agent-facing surface
  smoke: contract-lane    # or: off
  scenario: [epic-close, release, label]   # milestones; empty list = off
  critique: [layer-gate]  # or a cron string, or empty
  models: [claude, other] # families the scenario/critique tiers run on
```

Lite-profile repos default to `enabled: false`; MaC v1 is stamped with all three tiers on.

Rules that make these trustworthy:

- **Surface-only.** The agent has the same access a customer's agent would have: the published contract and credentials, nothing else. If it needs a checkout to succeed, that is the defect.
- **Model-plural.** Scenario and critique tiers run on at least two model families (one Claude, one not), because portability across agents is the claim being tested.
- **Persona and tasks are versioned assets** in `evals/agent/personas/` and `evals/agent/scenarios/`, reviewed like code, with the bot's instructions forbidding roadmap or issue-tracker access for the critique tier.
- **Evidence is kept.** Every run writes a transcript and a scored result under `evals/agent/evidence/<date>/`, so a finding can be traced to the exact call that produced it, and so acceptance (§2a) can be a recorded demonstration rather than a re-run.
- **Findings convert or expire.** A scenario failure files an issue; a critique finding becomes a proposal or is explicitly declined with a reason in the report. Nothing sits in a report unowned.

The blueprint ships the harness (runner, rubric schema, evidence writer, the persona and scenario file formats) and one reference persona in `examples/hello-contract/`; each project supplies its own personas and scenarios. Projects without an agent-facing surface, or that simply don't want the overhead, leave `agent_tests.enabled: false` and nothing in the harness runs or is required.

## 8. Versioning, licensing, release

**Versions stamped at release, zero commits.** Every package carries a `0.0.0-dev` placeholder; `release.yml` stamps the tag-derived version at build time and generates the changelog from conventional commits. This retires "update the version in the same commit" and the whole class of version-drift checks.

**Licensing by path.** `REUSE.toml` maps `**` to the default license and tier directories (`ee/**` or the project's equivalent) to the commercial one; `LICENSE` and `CONTRIBUTING` restate it. A project's open-core boundary is a directory, not a repo, which is what lets MaC v1 be one repo.

**Model selection is config.** A single `models.yaml` with tiers (`reasoning`, `logic`, `local`) and fallback chains, read by every agent script; the `pins` gate fails any literal model name outside that file.

## 9. Linear and process

The issue templates carry the Definition of Done as a checklist: proof path run, negative test added where applicable, consumers updated and `conformance` green, docs and decision references updated, status set to what is true. The PR template mirrors it. Since Linear auto-closes on merge, the `conformance` gate is the mechanical guard: a PR that changes `contract/` cannot merge with a red conformance lane, so the issue cannot auto-close with consumers behind.

`HANDOFF.md` is the convention for overnight and multi-agent builds: goal, what landed, what didn't, honest gaps, next command to run. The `.skills/handoff` skill writes it; the next agent reads it before anything else.

## 10. What is retired, and why it's safe

Conductor (`conductor.yml`, `decompose.py`, `CONDUCTOR_BUILD_PLAN.md`, `proposals/`, `processed-proposals/`): proposal-to-issue decomposition is replaced by the proposal → DR → spec → issue path, which Claude Code does in a session with full context rather than a one-shot LLM call with a hardcoded label. `reviewer-agent.yml` and `review_pr.py`: superseded by OCR for every consumer; the blueprint reviews itself with the same OCR stub. `close-completed-issues.yml`: dead one-off. `profiles/`, `Brewfile`, LiteLLM `config.yaml`, `setup-quality.sh`, `.vscode/`: machine setup, not repo structure; if Beau still wants a machine bootstrap it becomes a separate `beau-dev-machine` repo. The `.agents/workflows/*.md` recipes become `.skills/` where they still apply (`new-feature` → `write-a-spec`), and are dropped otherwise.

Nothing MaC v0 depends on changes: `ocr-review.yml` is untouched and stays at its current path, the consuming stubs keep their SHA pins, and `rollout-ocr.sh` keeps working. v0 repos are never re-stamped with v2; they stay frozen.

## 11. Delivery plan

Sequenced as PRs, each hold-for-Beau, each small enough to review in one sitting:

1. **Prune.** Delete the retired material, move machine bootstrap to `beau-dev-machine`, fix the README contradictions (`@main` vs SHA), close the six duplicate test issues. No behavior change to `ocr-review.yml`.
2. **Rules and skeleton.** `AGENTS.md`, `CODEOWNERS`, `project.yaml` profiles, `docs/` layout with schemas for DRs and specs, issue and PR templates, `HANDOFF.md`, `REUSE.toml`. Mark the repo as a template. The private flip is *not* part of this PR; it waits until no `beauzone/*` repo calls the workflow (§14, decision 3).
3. **Gates, first half.** `gates.yml` with `decisions`, `stale-refs`, `pins`, `hygiene`, `docs`, each with its negative test in `evals/`.
4. **Gates, second half.** `derived`, `conformance`, `boundaries`, `negative`, `security`. These need a reference implementation to test against, so they land alongside PR 6.
5. **Release.** `release.yml`, `models.yaml`, changelog generation.
6. **Reference project.** A minimal example (`examples/hello-contract/`) with one pure-domain package, one generated contract, one client, one spec, one DR, and green gates. This is what proves the template before MaC v1 uses it.
7. **Stamp MaC v1.** Create the new monorepo from the template; its first PR is the layout DR.

Effort: PRs 1–2 are a day; 3–5 are one overnight build each; 6 is two; 7 is the start of the reset. Roughly two weeks elapsed with review turnaround, and it front-runs the naming decision (nothing here depends on the name).

## 12. Project profiles (tracker and process tier)

A `project.yaml` at the repo root, written by `stamp.sh --profile full|lite --tracker linear|github` and read by AGENTS.md, the gates workflow, and the process skills.

- `tracker: linear | github` — where issues live. `plan-issues` creates Linear parent/child issues with blocking relations, or GitHub issues with a task-list parent and labels, and writes ids back into the spec either way. `stale-refs` resolves forward references against the chosen tracker. Commit refs become `Refs BEA-12` or `Refs #12`. The Linear status-discipline section of AGENTS.md is included only for `linear`.
- `profile: full | lite` — how much process. Full is everything in this document. Lite keeps what is cheap and protective (review, pins, hygiene, negative tests, `docs/decisions/`, a single `docs/spec.md`) and drops what only pays off at scale (layered roadmap, contract lane, conformance, boundary linter, evaluations, HANDOFF). `gates.yml` skips disabled jobs, so a lite repo's CI runs in about a minute.

Promotion is a one-line change plus re-running `stamp.sh`, which adds the missing skeleton without touching existing files. Defaults are `lite` + `github`; MaC v1 is stamped `full` + `linear`.

## 13. Decomposition: `plan-issues`

Replaces the Conductor. When a spec's status reaches *Contract frozen*, the `write-a-spec` skill hands off to `plan-issues`, which reads the spec's gates and exit test, creates one parent issue per spec and one child per gate or build unit, sets blocking relations from layer dependencies, flags children under hold-for-Beau paths, and writes the issue ids into the spec's status header. Because the spec carries the ids, re-running updates rather than duplicates. Invoked deliberately by a person or agent; never triggered by a file push.

## 14. Decisions taken 2026-09-06

1. **Machine bootstrap**: split into a separate `beau-dev-machine` repo (Brewfile, VS Code profile, local tooling). The blueprint carries project setup only.
2. **Conductor**: retired outright; replaced by `plan-issues` (§13).
3. **Ownership and visibility**: new MaC repos (monorepo, contract, registry) are created under `Beau-s-Dev-Org`. The blueprint becomes private with organization-wide reusable-workflow access **only after the v0 repos under `beauzone` no longer call it** — GitHub checks the called repository's visibility and owner at run time, so a SHA pin does not keep a cross-owner private workflow resolvable, and flipping early would fail every v0 repo's OCR review on its next PR (raised by the Codex review on PR #65). Until then the blueprint stays public. Two ways to reach the flip: archive the v0 repos at cutover (the default), or transfer them into `Beau-s-Dev-Org` earlier if their review pipeline needs to keep working past that point. `mac-contract` and the plugin mirror are public for outside consumers and do not depend on the blueprint.
4. **Language defaults**: Python and TypeScript, so every gate is proven for both toolchains and the reference example's client is in a second language to demonstrate contract neutrality.
5. **Project profiles**: `project.yaml` with `tracker` and `profile` (§12).
