# [Project Name] — AI Agent Instructions

## Project Overview

Brief description of the project and its core purpose.

## Architecture

- **Frontend:** [e.g., React, Next.js, Vite]
- **Backend:** [e.g., Node.js, Express, Python]
- **Database:** [e.g., PostgreSQL, MongoDB, DynamoDB]
- **Auth:** [e.g., Passport.js, Clerk, Auth0]

## Commands

- `npm run dev` — Start development server
- `npm run build` — Create production build
- `npm run test` — Run test suite

## Conventions

- UI library: [e.g., shadcn/ui, Tailwind CSS]
- State management: [e.g., React Query, Redux, Zustand]
- Data validation: [e.g., Zod, Joi]

## Git Workflow

- `main` branch is for production.
- Use `feature/*` branches for development.
- Require PRs for all merges to main.

## Review: local first, OCR last

Most review rounds happen locally, before a PR exists. The GitHub OCR review is
the final gate, so it should usually pass on its first run.

1. When a branch is ready, run the `local-review` skill (in the blueprint at
   `.skills/local-review/`). Orca runs reviewers from a **different model
   family** than the author, and the author fixes their findings round by round.
2. A branch is **clean** when no reviewer finds a P1/P2 on the head commit:
   one non-author family normally, two on hold-for-Beau paths.
   `scripts/local-review/local_review.py verdict` decides this, not the agent.
3. Only then open the PR, with the verdict's **Local review** section in the
   body. Pushing the branch earlier for backup is fine. OCR runs only on PR
   events.
4. OCR's review is the one the merge policy counts. If OCR finds a P1/P2, fix it
   and run a local round before pushing again.
5. If no local reviewer can run (every subscription is unavailable), open the PR
   anyway with the `unavailable` section, and OCR runs the rounds as it did
   before.

## Important Files

- `[file_path]` — Description of role
- `[file_path]` — Description of role

## Context for AI Agents

- Targeted User: [e.g., Retail traders, internal staff]
- Technical Level of Owner: [e.g., Non-engineer, Senior Dev]
- Core priorities: [e.g., Speed of delivery, high reliability, rich UX]
