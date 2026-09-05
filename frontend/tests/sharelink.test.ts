/**
 * Share-link codec tests.
 *
 * The policy under test — carried from the baseline audit — is that a malformed link is
 * rejected *entirely*, never salvaged. A half-applied share link produces a view that
 * looks deliberate and is not.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { decodeShareState, encodeShareState, type ShareState } from '../src/state/sharelink.ts';

function state(overrides: Partial<ShareState> = {}): ShareState {
  return {
    eventId: null,
    entityId: null,
    scenarioId: null,
    camera: null,
    path: [],
    showDependencies: true,
    viewMode: 'executive',
    ...overrides,
  };
}

describe('share links', () => {
  it('round-trips a full state', () => {
    const original = state({
      eventId: 'replay:taiwan-m68',
      entityId: 'payments-k8s-singapore',
      scenarioId: 'sim-abc123',
      camera: { lat: 24.61, lon: 121.03, height: 900000, heading: 42, pitch: -55 },
      path: ['supplier-taiwan-hardware', 'payments-k8s-singapore', 'payments-api'],
      showDependencies: false,
      viewMode: 'engineer',
    });
    const decoded = decodeShareState(encodeShareState(original));
    assert.ok(decoded);
    assert.equal(decoded.eventId, original.eventId);
    assert.equal(decoded.entityId, original.entityId);
    assert.equal(decoded.scenarioId, original.scenarioId);
    assert.deepEqual(decoded.path, original.path);
    assert.equal(decoded.showDependencies, false);
    assert.equal(decoded.viewMode, 'engineer');
    assert.equal(decoded.camera?.lat.toFixed(2), '24.61');
    assert.equal(decoded.camera?.heading, 42);
  });

  it('produces an empty query for empty state', () => {
    assert.equal(encodeShareState(state()).toString(), '');
  });

  it('decodes an empty query as empty state, not as a failure', () => {
    const decoded = decodeShareState('');
    assert.ok(decoded);
    assert.equal(decoded.eventId, null);
    assert.equal(decoded.showDependencies, true);
  });

  it('never emits anything credential-shaped', () => {
    const query = encodeShareState(
      state({ eventId: 'replay:taiwan-m68', scenarioId: 'sim-abc' }),
    ).toString();
    assert.doesNotMatch(query, /key|token|secret|password/i);
  });

  for (const [label, query] of [
    ['a malformed id', 'e=not a valid id!!'],
    ['a script-ish id', 'n=<script>alert(1)</script>'],
    ['a truncated camera', 'c=1,2,3'],
    ['a non-numeric camera', 'c=a,b,c,d,e'],
    ['an out-of-range latitude', 'c=999,0,1000,0,-45'],
    ['an out-of-range longitude', 'c=0,999,1000,0,-45'],
    ['an absurd camera height', 'c=0,0,999999999999,0,-45'],
    ['an out-of-range pitch', 'c=0,0,1000,0,-200'],
    ['a bad hop in the path', 'p=good-id,bad id!!,other'],
    ['an unknown dependency flag', 'd=maybe'],
    ['an unknown view mode', 'm=wizard'],
  ] as const) {
    it(`rejects ${label} entirely`, () => {
      assert.equal(decodeShareState(query), null, 'must reject, not salvage');
    });
  }

  it('rejects an oversized parameter', () => {
    assert.equal(decodeShareState(`e=${'x'.repeat(600)}`), null);
  });

  it('rejects a path longer than the hop ceiling', () => {
    const hops = Array.from({ length: 30 }, (_, index) => `n${index}`).join(',');
    assert.equal(decodeShareState(`p=${hops}`), null);
  });

  it('normalizes a heading past 360', () => {
    const decoded = decodeShareState('c=0,0,1000,725,-45');
    assert.ok(decoded);
    assert.equal(decoded.camera?.heading, 5);
  });
});
