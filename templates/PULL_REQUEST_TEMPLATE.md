## What and why

<!-- One or two sentences. Refs BEA-XX. -->

## Local review

<!--
Paste the output of:
  local_review.py verdict ... --markdown --rounds <N>
or, when no local reviewer could run:
  local_review.py unavailable --reason "..."
Then add one line per earlier round: what it found and what was fixed.
See .skills/local-review/SKILL.md in beau-dev-blueprint.
-->

## Proof

<!-- The test run, command output, or live check that proves this works. "Should work" is not a result. -->

- [ ] Tests run, with the pass count stated
- [ ] Negative test added for any new gate, validator, or check
- [ ] Consumers updated where an interface changed
