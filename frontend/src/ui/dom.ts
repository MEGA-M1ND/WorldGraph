/**
 * DOM helpers.
 *
 * One rule this module exists to enforce: **feed text is never injected as markup.**
 * Every string that came from outside — an event title, a CVE description, a supplier
 * note — is set with `textContent`. `innerHTML` appears nowhere in this codebase.
 */

type Attrs = Record<string, string | number | boolean | null | undefined>;

/** Create an element with attributes and children in one call. */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: (Node | string | null | undefined | false)[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = String(value);
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'html') throw new Error('innerHTML is not permitted — use text');
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

/** Replace an element's children. */
export function replaceChildren(target: Element, ...children: (Node | null | undefined)[]): void {
  target.replaceChildren(...children.filter((child): child is Node => Boolean(child)));
}

/** Look up a `data-bind` slot, throwing loudly if the markup and code have drifted. */
export function bind<T extends Element = HTMLElement>(name: string, root: ParentNode = document): T {
  const node = root.querySelector<T>(`[data-bind="${name}"]`);
  if (!node) throw new Error(`Missing [data-bind="${name}"] in the document`);
  return node;
}

export function bindAll<T extends Element = HTMLElement>(
  selector: string,
  root: ParentNode = document,
): T[] {
  return Array.from(root.querySelectorAll<T>(selector));
}

// ------------------------------------------------------------------------------------
// Formatting
// ------------------------------------------------------------------------------------

/** `99.97%` — availability and traffic figures, always to two decimals. */
export function percent(value: number, decimals = 2): string {
  return `${(value * 100).toFixed(decimals)}%`;
}

/** `63%` — capacity and impact figures, where a decimal point is false precision. */
export function roundPercent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

/** Compact currency. Always a modelled figure, never presented as accounting. */
export function money(value: number): string {
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 1_000) return `$${Math.round(value / 1_000)}K`;
  return `$${Math.round(value).toLocaleString('en-US')}`;
}

export function count(value: number): string {
  return value.toLocaleString('en-US');
}

/** `09:42:17` in UTC — the console's clock is UTC everywhere, no local-time ambiguity. */
export function utcTime(iso: string | number | Date): string {
  return new Date(iso).toISOString().slice(11, 19);
}

export function utcClock(date = new Date()): string {
  return `${date.toISOString().slice(11, 16)} UTC`;
}

/** `4 minutes ago` — freshness, which is the one thing that must never read as absolute. */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

/** `4 minutes` — a duration without the "ago", for freshness rows. */
export function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'unknown';
  if (seconds < 60) return `${Math.round(seconds)} seconds`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} minutes`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)} hours`;
  return `${Math.round(seconds / 86_400)} days`;
}

/** `KUBERNETES_CLUSTER` → `Kubernetes cluster`. */
export function humanize(value: string): string {
  const lower = value.replace(/_/g, ' ').toLowerCase();
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}

// ------------------------------------------------------------------------------------
// Small shared components
// ------------------------------------------------------------------------------------

export function badge(text: string, severity?: string): HTMLElement {
  return el('span', { class: 'badge', 'data-severity': severity ?? null, text });
}

export function stateBadge(text: string, state: string): HTMLElement {
  return el('span', { class: 'badge', 'data-state': state, text });
}

export function keyValue(pairs: [string, Node | string, string?][]): HTMLElement {
  const grid = el('div', { class: 'kv' });
  for (const [key, value, attribute] of pairs) {
    grid.append(el('span', { class: 'kv__key', text: key }));
    const cell = el('span', { class: 'kv__value' });
    if (attribute) {
      // Severity and state attributes drive the colour tokens; see tokens.css.
      if (['CRITICAL', 'HIGH', 'MODERATE', 'LOW', 'INFO'].includes(attribute)) {
        cell.setAttribute('data-severity', attribute);
      } else {
        cell.setAttribute('data-state', attribute);
      }
    }
    cell.append(typeof value === 'string' ? document.createTextNode(value) : value);
    grid.append(cell);
  }
  return grid;
}

export function section(title: string, ...children: (Node | null)[]): HTMLElement {
  return el(
    'section',
    { class: 'detail__section' },
    el('h3', { class: 'detail__section-title', text: title }),
    ...children,
  );
}

/** A signed risk contribution row: `+30  critical asset impacted`. */
export function derivation(
  contributions: { label: string; points: number; detail: string }[],
): HTMLElement {
  const grid = el('div', { class: 'derivation' });
  for (const item of contributions) {
    grid.append(
      el('span', {
        class: 'derivation__points',
        'data-sign': item.points >= 0 ? 'positive' : 'negative',
        text: `${item.points >= 0 ? '+' : ''}${Math.round(item.points)}`,
      }),
      el('span', { class: 'derivation__label', text: item.label }),
    );
    if (item.detail) {
      grid.append(el('span', { class: 'derivation__detail', text: item.detail }));
    }
  }
  return grid;
}

/** A dependency path as a chain of hop chips. */
export function pathChain(
  hops: { entity_name: string; availability: number }[],
): HTMLElement {
  const wrapper = el('div', { class: 'path' });
  hops.forEach((hop, index) => {
    if (index > 0) wrapper.append(el('span', { class: 'path__arrow', text: '→' }));
    wrapper.append(
      el('span', {
        class: 'path__hop',
        'data-degraded': hop.availability < 0.999 ? 'true' : 'false',
        text: hop.entity_name,
      }),
    );
  });
  return wrapper;
}

/** Confidence evidence, marked `+` for supporting and `?` for uncertain. */
export function evidenceList(strong: string[], uncertain: string[]): HTMLElement {
  const list = el('ul', { class: 'evidence' });
  for (const item of strong) {
    list.append(el('li', { 'data-kind': 'strong', 'data-marker': '+', text: item }));
  }
  for (const item of uncertain) {
    list.append(el('li', { 'data-kind': 'uncertain', 'data-marker': '?', text: item }));
  }
  return list;
}

export function disclaimer(text = 'Modelled estimate — synthetic data'): HTMLElement {
  return el('p', { class: 'disclaimer', text });
}
