## What this changes

<!-- The behaviour that is different after this merges. Not a file list. -->

## Why

<!-- The problem. If a test or a browser pass found it, say so and quote the evidence. -->

## Honesty checklist

WorldGraph's central claim is that it does not invent things. These are the ways that
claim usually breaks:

- [ ] No number is produced without evidence (missing metadata yields `UNKNOWN`, not `0`)
- [ ] Every new dependency edge carries provenance and a defensible method
- [ ] Simulated / synthetic / replayed data cannot render as `LIVE`
- [ ] External text (feed, tag, description) is treated as data, never as instructions
- [ ] No credential can reach the browser, a log, or a URL
- [ ] Nothing operational is executed — recommendations only

## Validation

- [ ] `cd backend && ruff check app tests && pytest`
- [ ] `cd frontend && npm run typecheck && npm test && npm run build`
- [ ] `cd frontend && npx playwright test` (backend running)
- [ ] Screenshots inspected, if the UI changed
