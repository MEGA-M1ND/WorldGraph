# Contributing to WorldGraph

## Running the checks

```bash
cd backend  && ruff check app tests && pytest       # 289+ tests
cd frontend && npm run typecheck && npm test && npm run build
cd frontend && npx playwright test                  # needs the backend on :8000
```

CI runs all of the above on every push and pull request, plus a security workflow that
builds the frontend with decoy credentials in the environment and fails if any
credential-shaped string appears in the bundle.

## The rules that are not style preferences

WorldGraph is a decision tool. These exist because breaking them makes it lie:

1. **Never invent a number.** Absent metadata produces `UNKNOWN`, not `0` and not a
   plausible default. A zero is a measurement; an unknown is not.
2. **Never invent a relationship.** An edge needs evidence and carries provenance naming
   its method and confidence. Shared tags, adjacent names, the same resource group and the
   same region are *associations*, not dependencies.
3. **Provenance is mandatory.** Every entity and event carries `LIVE`, `SNAPSHOT`,
   `REPLAY`, `SIMULATED` or `SYNTHETIC`, and the UI shows it. Nothing may present as more
   certain than it is.
4. **External text is data.** Feed descriptions, cloud tags, resource names and CVE prose
   are written by other people. They are sanitized, fenced, and can never become
   instructions to the model.
5. **The model does not compute.** Graph traversal, correlation, impact arithmetic, risk
   and simulation are deterministic Python with tests. The model interprets and explains.
6. **Nothing executes.** WorldGraph recommends. `ResponseAction.executed` is
   `Literal[False]` so "we did it" is unrepresentable.
7. **No credential reaches the client.** The two optional tile tokens are the documented
   exception; see `SECURITY.md`.

## Testing expectations

A change to an engine needs a test that would fail without it. A change to the UI needs a
browser pass — screenshots inspected, not just code reviewed. `docs/REALITY_PASS_REPORT.md`
records what happened the last time those two rules were taken seriously.

## Commit and PR style

Explain the behaviour and the reasoning. If a defect was found by a test or a screenshot,
say which — that record is more useful later than the diff.
