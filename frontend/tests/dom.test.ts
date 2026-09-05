/**
 * Formatting and DOM-helper tests.
 *
 * The one that matters most is the `innerHTML` guard: every string WorldGraph renders may
 * have come from an external feed, and `el()` is the single chokepoint that keeps them
 * text.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import { UNKNOWN, age, count, humanize, money, percent, roundPercent, utcTime } from '../src/ui/dom.ts';

/**
 * The distinction these tests defend: a figure WorldGraph could not compute arrives as
 * `null`, and rendering it as `0`, `$0` or `100.00%` would state a fact nobody
 * established. The Reality Pass found the panels doing exactly that for an estate that
 * declared no business metadata (docs/REALITY_PASS_AUDIT.md, C1).
 */
describe('missing measurements', () => {
  it('renders a null availability as UNKNOWN, not 100%', () => {
    assert.equal(percent(null), UNKNOWN);
    assert.equal(roundPercent(null), UNKNOWN);
  });

  it('renders a null revenue as UNKNOWN, not $0', () => {
    assert.equal(money(null), UNKNOWN);
  });

  it('renders a null count as UNKNOWN, not 0', () => {
    assert.equal(count(null), UNKNOWN);
  });

  it('treats undefined the same as null', () => {
    assert.equal(percent(undefined), UNKNOWN);
    assert.equal(money(undefined), UNKNOWN);
    assert.equal(count(undefined), UNKNOWN);
  });

  it('still renders a real zero as a zero', () => {
    // Zero is a measurement. It must not be confused with the absence of one.
    assert.equal(percent(0), '0.00%');
    assert.equal(money(0), '$0');
    assert.equal(count(0), '0');
  });
});

describe('formatting', () => {
  it('formats availability to two decimals', () => {
    assert.equal(percent(0.9997), '99.97%');
    assert.equal(percent(1), '100.00%');
    assert.equal(percent(0), '0.00%');
  });

  it('rounds capacity figures, where a decimal is false precision', () => {
    assert.equal(roundPercent(0.6294), '63%');
    assert.equal(roundPercent(1), '100%');
  });

  it('formats money compactly', () => {
    assert.equal(money(3_260_000), '$3.26M');
    assert.equal(money(382_000), '$382K');
    assert.equal(money(0), '$0');
    assert.equal(money(950), '$950');
  });

  it('formats counts with separators', () => {
    assert.equal(count(41500), '41,500');
  });

  it('renders times in UTC', () => {
    assert.equal(utcTime('2026-09-05T09:42:17Z'), '09:42:17');
  });

  it('describes ages in the largest sensible unit', () => {
    assert.equal(age(45), '45 seconds');
    assert.equal(age(240), '4 minutes');
    assert.equal(age(7200), '2 hours');
    assert.equal(age(null), 'unknown');
  });

  it('humanizes enum values', () => {
    assert.equal(humanize('KUBERNETES_CLUSTER'), 'Kubernetes cluster');
    assert.equal(humanize('SECURITY_VULNERABILITY'), 'Security vulnerability');
  });
});
