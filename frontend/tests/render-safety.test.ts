/**
 * The claim `src/ui/dom.ts` makes about itself, checked.
 *
 * That module's docstring says "feed text is never injected as markup" and "`innerHTML`
 * appears nowhere in this codebase", and `dom.test.ts` opens by calling the `el()` guard
 * "the one that matters most". Nothing tested it. Every string WorldGraph renders may have
 * arrived from an external feed — an event title, a CVE description, a supplier note — and
 * `el()` is the single chokepoint that keeps them text.
 *
 * Two checks, because one is not enough:
 *
 * 1. **The chokepoint behaves.** `el()` is exercised against a recording stand-in for the
 *    DOM, which reports *which sink* each value reached. That is the whole question here —
 *    `textContent` versus `innerHTML` — so a stand-in that records the sink answers it.
 *    It does not prove anything about a real browser's parsing; the E2E suite does that.
 * 2. **Nothing bypasses it.** A source scan over `src/` for the markup sinks, so the
 *    module's claim about the codebase is a test rather than a comment. Adding
 *    `innerHTML` anywhere in the app now fails here.
 */

import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it } from 'node:test';

const SRC = join(fileURLToPath(new URL('../src', import.meta.url)));

/** A recording element. Every write says which sink it went to. */
class FakeElement {
  tag: string;
  className = '';
  textContent = '';
  attributes: Record<string, string> = {};
  children: unknown[] = [];
  /** Assigned only if production code ever reaches for it. */
  innerHTML: string | undefined;

  constructor(tag: string) {
    this.tag = tag;
  }

  setAttribute(key: string, value: string): void {
    this.attributes[key] = value;
  }

  append(...nodes: unknown[]): void {
    this.children.push(...nodes);
  }
}

class FakeText {
  data: string;
  constructor(data: string) {
    this.data = data;
  }
}

(globalThis as unknown as { document: unknown }).document = {
  createElement: (tag: string) => new FakeElement(tag),
  createTextNode: (data: string) => new FakeText(data),
};

const { el } = await import('../src/ui/dom.ts');

const PAYLOAD = '<img src=x onerror="alert(1)">';

describe('el() is the chokepoint for feed text', () => {
  it('routes a text attribute to textContent and never to innerHTML', () => {
    const node = el('div', { text: PAYLOAD }) as unknown as FakeElement;
    assert.equal(node.textContent, PAYLOAD);
    assert.equal(node.innerHTML, undefined);
  });

  it('turns a string child into a text node rather than markup', () => {
    const node = el('div', {}, PAYLOAD) as unknown as FakeElement;
    assert.equal(node.children.length, 1);
    assert.ok(node.children[0] instanceof FakeText);
    assert.equal((node.children[0] as FakeText).data, PAYLOAD);
    assert.equal(node.innerHTML, undefined);
  });

  it('refuses an html attribute outright', () => {
    // Loudly, at the call site, rather than quietly rendering it.
    assert.throws(() => el('div', { html: '<b>x</b>' }), /innerHTML is not permitted/);
  });

  it('still does the ordinary work: class, attributes, flags', () => {
    const node = el('a', {
      class: 'link',
      href: 'https://example.invalid',
      'data-action': 'open',
      hidden: true,
      title: null,
      alt: undefined,
      tabindex: 0,
      draggable: false,
    }) as unknown as FakeElement;
    assert.equal(node.className, 'link');
    assert.equal(node.attributes.href, 'https://example.invalid');
    assert.equal(node.attributes['data-action'], 'open');
    // `true` becomes a bare attribute, the HTML spelling of a flag.
    assert.equal(node.attributes.hidden, '');
    // null, undefined and false are omitted — not rendered as the strings "null"/"false".
    assert.equal('title' in node.attributes, false);
    assert.equal('alt' in node.attributes, false);
    assert.equal('draggable' in node.attributes, false);
    // 0 is a value, not an absence.
    assert.equal(node.attributes.tabindex, '0');
  });

  it('skips empty children without collapsing the real ones', () => {
    const child = el('span', { text: 'kept' });
    const node = el('div', {}, null, undefined, false, child, 'also kept') as unknown as FakeElement;
    assert.equal(node.children.length, 2);
  });
});

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...sourceFiles(path));
    else if (entry.name.endsWith('.ts')) found.push(path);
  }
  return found;
}

describe('nothing in the app bypasses the chokepoint', () => {
  /** Sinks that turn a string into markup or into code. */
  const FORBIDDEN = [
    'innerHTML',
    'outerHTML',
    'insertAdjacentHTML',
    'document.write',
    'eval(',
    'new Function(',
  ];

  it('finds the source tree it is supposed to be scanning', () => {
    // Otherwise an empty scan would pass as a clean one.
    const files = sourceFiles(SRC);
    assert.ok(files.length >= 5, `expected several source files, found ${files.length}`);
    assert.ok(files.some((f) => f.endsWith('main.ts')));
  });

  it('contains no markup or code sink outside the guard itself', () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const lines = readFileSync(file, 'utf8').split('\n');
      lines.forEach((line, index) => {
        // The guard in dom.ts names the thing it forbids, in a comment and in the message
        // it throws. Those two are the exception; nothing else is.
        const isTheGuard = file.endsWith('dom.ts') && line.includes('not permitted');
        const isProse = line.trimStart().startsWith('*');
        if (isTheGuard || isProse) return;
        for (const sink of FORBIDDEN) {
          if (line.includes(sink)) offenders.push(`${file}:${index + 1} ${sink}`);
        }
      });
    }
    assert.deepEqual(offenders, []);
  });
});
