/**
 * Panel renderers.
 *
 * Each function takes state and returns DOM. They are pure with respect to the store —
 * they read, they never write — so the render pass has no ordering hazards, and every
 * interaction is delivered through the callbacks in `PanelActions`.
 */

import {
  age,
  badge,
  count,
  derivation,
  disclaimer,
  el,
  evidenceList,
  humanize,
  keyValue,
  money,
  pathChain,
  percent,
  relativeTime,
  roundPercent,
  section,
  stateBadge,
  utcTime,
} from './dom.ts';
import type { AppState } from '../state/store.ts';
import type {
  BlastRadiusResult,
  EntityDetail,
  EventDetail,
  FeedStatus,
  MaterialRisk,
  ResponsePlan,
  SimulationComparison,
  TimelineEntry,
  WorldEvent,
} from '../types.ts';

export interface PanelActions {
  selectEvent: (eventId: string) => void;
  selectEntity: (entityId: string) => void;
  focusRisk: (risk: MaterialRisk) => void;
  analyzeEvent: (eventId: string) => void;
  traceDependencies: (entityId: string) => void;
  simulateFailure: (entityId: string) => void;
  askAbout: (entityId: string) => void;
  removeOverride: (overrideId: string) => void;
  generatePlan: () => void;
}

// ------------------------------------------------------------------------------------
// Headline stats
// ------------------------------------------------------------------------------------

export function renderHeadlineStats(state: AppState): HTMLElement[] {
  const { dashboard, metrics, comparison, simulationActive } = state;
  if (!dashboard) return [];

  // In simulation mode the headline numbers show the *simulated* world, badged, so the
  // top bar can never quietly display a hypothetical as the current state.
  const live = simulationActive && comparison ? comparison.simulated : metrics;
  const availability = live?.availability ?? dashboard.availability;
  const risk = live?.material_risk ?? 'LOW';

  const stats: [string, string, string?][] = [
    ['Critical services', count(dashboard.critical_services)],
    ['Infrastructure', count(dashboard.infrastructure_assets)],
    ['Active incidents', count(dashboard.active_incidents)],
    ['Material risks', count(dashboard.material_risks)],
    ['Availability', percent(availability)],
    ['Risk', risk, risk],
  ];

  return stats.map(([label, value, severity]) =>
    el(
      'div',
      { class: 'stat' },
      el('span', { class: 'stat__value', 'data-severity': severity ?? null, text: value }),
      el('span', { class: 'stat__label', text: label }),
    ),
  );
}

// ------------------------------------------------------------------------------------
// Material risks
// ------------------------------------------------------------------------------------

export function renderRisks(state: AppState, actions: PanelActions): HTMLElement {
  const container = el('div');
  if (state.risks.length === 0) {
    container.append(el('p', { class: 'empty-state', text: 'No standing material risks.' }));
    return container;
  }
  for (const risk of state.risks) {
    const row = el(
      'button',
      { type: 'button', class: 'row', 'data-severity': risk.severity },
      el(
        'div',
        { class: 'row__head' },
        badge(risk.severity, risk.severity),
        el('span', { class: 'row__title', text: risk.title }),
      ),
      el('p', { class: 'row__summary', text: risk.summary }),
    );
    row.addEventListener('click', () => actions.focusRisk(risk));
    container.append(row);
  }
  return container;
}

// ------------------------------------------------------------------------------------
// Event feed
// ------------------------------------------------------------------------------------

export function renderEvents(state: AppState, actions: PanelActions): HTMLElement {
  const container = el('div');
  if (state.events.length === 0) {
    container.append(
      el('p', {
        class: 'empty-state',
        text: 'No world events ingested. Check the data-source panel below.',
      }),
    );
    return container;
  }

  for (const event of state.events) {
    const row = el(
      'button',
      {
        type: 'button',
        class: `row${state.selectedEventId === event.id ? ' is-selected' : ''}`,
        'data-severity': event.severity,
        'data-event-id': event.id,
      },
      el(
        'div',
        { class: 'row__head' },
        badge(event.severity, event.severity),
        // Provenance sits beside severity on every single row. An operator must never
        // have to hunt for whether a thing on their globe is real.
        stateBadge(event.source.mode, event.source.mode),
      ),
      el('div', { class: 'row__title', text: event.title }),
      el(
        'div',
        { class: 'row__meta' },
        el('span', { text: humanize(event.category) }),
        el('span', { text: relativeTime(event.occurred_at) }),
        el('span', { text: event.source.source_name }),
      ),
    );
    row.addEventListener('click', () => actions.selectEvent(event.id));
    container.append(row);
  }
  return container;
}

// ------------------------------------------------------------------------------------
// Feed status
// ------------------------------------------------------------------------------------

export function renderFeeds(feeds: FeedStatus[]): HTMLElement {
  const container = el('div');
  if (feeds.length === 0) {
    container.append(el('p', { class: 'empty-state', text: 'No data sources registered.' }));
    return container;
  }
  for (const feed of feeds) {
    container.append(
      el(
        'div',
        { class: 'feed', 'data-state': feed.state },
        el('span', { class: 'feed__dot' }),
        el('span', { class: 'feed__name', text: feed.adapter_name }),
        el('span', { class: 'feed__state', text: feed.state }),
        el('span', { class: 'feed__age', text: relativeTime(feed.last_success_at) }),
      ),
    );
    // A degraded feed says exactly what is wrong, in the row, without a click.
    if (feed.message) {
      container.append(el('div', { class: 'feed__message', text: feed.message }));
    }
  }
  return container;
}

// ------------------------------------------------------------------------------------
// Event detail
// ------------------------------------------------------------------------------------

export function renderEventDetail(
  detail: EventDetail,
  analysis: BlastRadiusResult | null,
  actions: PanelActions,
): HTMLElement {
  const { event } = detail;
  const container = el('div');

  container.append(
    el(
      'header',
      { class: 'detail__header' },
      el('h3', { class: 'detail__name', text: event.title }),
      el(
        'div',
        { class: 'detail__badges' },
        badge(event.severity, event.severity),
        stateBadge(event.source.mode, event.source.mode),
        badge(humanize(event.category)),
      ),
    ),
  );

  const magnitude = event.metadata['magnitude'];
  const pairs: [string, string, string?][] = [
    ['Occurred', `${utcTime(event.occurred_at)} UTC`],
    ['Source', event.source.source_name],
    ['Freshness', age(detail.freshness_seconds)],
    ['State', event.source.mode, event.source.mode],
  ];
  if (typeof magnitude === 'number') pairs.splice(1, 0, ['Magnitude', `M${magnitude.toFixed(1)}`]);
  if (event.exposure_radius_km > 0) {
    pairs.push(['Exposure radius', `${Math.round(event.exposure_radius_km)} km`]);
  }
  container.append(section('Provenance', keyValue(pairs)));

  if (event.description) {
    container.append(
      section(
        'Description',
        // External text. Set as textContent by `el`, never as markup.
        el('p', { class: 'row__summary', style: '-webkit-line-clamp:12', text: event.description }),
      ),
    );
  }

  const proximity = detail.proximity;
  if (detail.assets_in_radius.length > 0) {
    const list = el('div');
    for (const asset of detail.assets_in_radius) {
      const pill = el('button', {
        type: 'button',
        class: 'pill',
        text: `${asset.name} · ${Math.round(asset.distance_km)} km ${asset.direction}`,
      });
      pill.addEventListener('click', () => actions.selectEntity(asset.entity_id));
      list.append(pill);
    }
    container.append(
      section(
        'Enterprise proximity',
        keyValue([
          ['Facilities', count(proximity.critical_facilities ?? 0)],
          ['Suppliers', count(proximity.suppliers ?? 0)],
          ['Dependent services', count(proximity.dependent_services ?? 0)],
        ]),
        el('div', { class: 'pill-list', style: 'margin-top:8px' }, list),
      ),
    );
  }

  if (detail.vulnerable_assets.length > 0) {
    const list = el('div', { class: 'pill-list' });
    for (const asset of detail.vulnerable_assets) {
      const pill = el('button', {
        type: 'button',
        class: 'pill',
        text: `${asset.name}${asset.internet_facing ? ' · internet-facing' : ''}`,
      });
      pill.addEventListener('click', () => actions.selectEntity(asset.entity_id));
      list.append(pill);
    }
    container.append(section(`Affected assets (${detail.vulnerable_assets.length})`, list));
  }

  const analyse = el('button', {
    type: 'button',
    class: 'primary-button',
    text: analysis?.origin_kind === 'EVENT' ? 'Re-analyse impact' : 'Analyze impact',
  });
  analyse.addEventListener('click', () => actions.analyzeEvent(event.id));
  container.append(el('div', { class: 'detail__actions' }, analyse));

  if (analysis) container.append(renderAnalysis(analysis, actions));
  return container;
}

// ------------------------------------------------------------------------------------
// Blast radius
// ------------------------------------------------------------------------------------

export function renderAnalysis(
  analysis: BlastRadiusResult,
  actions: PanelActions,
): HTMLElement {
  const container = el('div');
  const impact = analysis.business_impact;

  container.append(
    section(
      `${analysis.severity} material risk`,
      el(
        'div',
        { class: 'detail__badges', style: 'margin-bottom:8px' },
        badge(`${Math.round(analysis.risk.score)}/100`, analysis.severity),
        stateBadge(analysis.mode, analysis.mode),
      ),
      derivation(analysis.risk.contributions),
    ),
  );

  if (analysis.direct_impact.length > 0) {
    container.append(
      section(
        `Directly exposed (${analysis.direct_impact.length})`,
        impactList(analysis.direct_impact, actions),
      ),
    );
  }
  if (analysis.indirect_impact.length > 0) {
    container.append(
      section(
        `Indirectly exposed (${analysis.indirect_impact.length})`,
        impactList(analysis.indirect_impact, actions),
      ),
    );
  }

  if (analysis.critical_paths.length > 0) {
    const paths = el('div');
    for (const path of analysis.critical_paths.slice(0, 4)) paths.append(pathChain(path.hops));
    container.append(section('Critical paths', paths));
  }

  if (analysis.customer_exposure.length > 0) {
    container.append(
      section(
        'Customer exposure',
        keyValue(
          analysis.customer_exposure.map(
            (row) =>
              [
                row.region,
                `${roundPercent(row.traffic_impact)} traffic · ${count(row.customer_count)} customers`,
              ] as [string, string],
          ),
        ),
      ),
    );
  }

  container.append(
    section(
      'Business impact',
      keyValue([
        ['Availability', percent(impact.availability)],
        ['Traffic impacted', roundPercent(impact.traffic_impact)],
        ['Customers', count(impact.customers_affected)],
        ['Revenue at risk', `${money(impact.revenue_at_risk_per_hour)}/hr`],
        ['Critical services', count(impact.critical_services_impacted)],
        ['SLA breaches', count(impact.sla_breaches.length)],
      ]),
      disclaimer(`${impact.disclaimer} — WorldGraph V1 Impact Model over synthetic data`),
    ),
  );

  container.append(
    section(
      `Confidence ${Math.round(analysis.confidence.score * 100)}%`,
      evidenceList(analysis.confidence.strong_evidence, analysis.confidence.uncertainties),
    ),
  );

  if (analysis.truncated) {
    container.append(
      el('p', {
        class: 'empty-state',
        text: `Traversal truncated: ${analysis.truncation_reason ?? 'bound reached'}. Impact may extend further.`,
      }),
    );
  }

  const plan = el('button', { type: 'button', class: 'primary-button', text: 'Generate response plan' });
  plan.addEventListener('click', () => actions.generatePlan());
  container.append(el('div', { class: 'detail__actions' }, plan));
  return container;
}

function impactList(
  rows: BlastRadiusResult['direct_impact'],
  actions: PanelActions,
): HTMLElement {
  const list = el('div');
  for (const row of rows.slice(0, 10)) {
    const button = el(
      'button',
      { type: 'button', class: 'row' },
      el(
        'div',
        { class: 'row__head' },
        el('span', { class: 'row__title', text: row.entity_name }),
        badge(roundPercent(row.availability), availabilitySeverity(row.availability)),
      ),
      el(
        'div',
        { class: 'row__meta' },
        el('span', { text: humanize(row.entity_type) }),
        el('span', { text: `depth ${row.depth}` }),
        row.customer_facing ? el('span', { text: 'customer-facing' }) : null,
      ),
    );
    button.addEventListener('click', () => actions.selectEntity(row.entity_id));
    list.append(button);
  }
  if (rows.length > 10) {
    list.append(el('p', { class: 'empty-state', text: `+ ${rows.length - 10} more` }));
  }
  return list;
}

function availabilitySeverity(availability: number): string {
  if (availability < 0.3) return 'CRITICAL';
  if (availability < 0.7) return 'HIGH';
  if (availability < 0.999) return 'MODERATE';
  return 'INFO';
}

// ------------------------------------------------------------------------------------
// Entity detail
// ------------------------------------------------------------------------------------

export function renderEntityDetail(
  detail: EntityDetail,
  state: AppState,
  actions: PanelActions,
): HTMLElement {
  const { entity } = detail;
  const container = el('div');
  const name = (id: string) => state.entitiesById.get(id)?.name ?? id;

  container.append(
    el(
      'header',
      { class: 'detail__header' },
      el('h3', { class: 'detail__name', text: entity.name }),
      el(
        'div',
        { class: 'detail__badges' },
        badge(entity.criticality, criticalitySeverity(entity.criticality)),
        stateBadge(entity.source.mode, entity.source.mode),
        badge(humanize(entity.type)),
      ),
    ),
  );

  if (entity.description) {
    container.append(el('p', { class: 'row__summary', style: '-webkit-line-clamp:6', text: entity.description }));
  }

  const facts: [string, string, string?][] = [
    ['Status', detail.availability >= 0.999 ? 'HEALTHY' : `${roundPercent(detail.availability)} available`],
    ['Capacity', roundPercent(detail.capacity)],
    ['Criticality', entity.criticality, criticalitySeverity(entity.criticality)],
  ];
  if (entity.business.region) facts.push(['Region', entity.business.region]);
  if (entity.business.sla_tier) facts.push(['SLA tier', entity.business.sla_tier]);
  if (entity.business.traffic_share > 0) {
    facts.push(['Traffic share', roundPercent(entity.business.traffic_share)]);
  }
  facts.push(['Redundancy', entity.business.redundancy === 1 ? '1 (single point)' : String(entity.business.redundancy)]);
  if (entity.exposure.internet_facing) facts.push(['Exposure', 'INTERNET-FACING', 'DEGRADED']);
  container.append(section('Status', keyValue(facts)));

  if (detail.hosts.length > 0) {
    container.append(section('Hosts', pillList(detail.hosts, name, actions)));
  }
  if (detail.depends_on.length > 0) {
    container.append(
      section('Depends on', pillList(detail.depends_on.map((e) => e.target_entity_id), name, actions)),
    );
  }
  if (detail.dependents.length > 0) {
    container.append(
      section('Dependents', pillList(detail.dependents.map((e) => e.source_entity_id), name, actions)),
    );
  }

  if (entity.software.length > 0) {
    const list = el('div', { class: 'pill-list' });
    for (const component of entity.software) {
      list.append(
        el('span', {
          class: 'pill',
          text: `${component.name} ${component.version}${component.cve_ids.length ? ` · ${component.cve_ids.join(', ')}` : ''}`,
        }),
      );
    }
    container.append(section('Software', list));
  }

  container.append(
    section(
      'Signals',
      keyValue([
        ['Current risks', count(detail.risk_count)],
        ['Recent events', count(detail.recent_event_ids.length)],
        ['Source', entity.source.source_name],
      ]),
    ),
  );

  const actionRow = el('div', { class: 'detail__actions' });
  const buttons: [string, () => void][] = [
    ['Trace dependencies', () => actions.traceDependencies(entity.id)],
    ['Simulate failure', () => actions.simulateFailure(entity.id)],
    ['Ask AI', () => actions.askAbout(entity.id)],
  ];
  for (const [label, handler] of buttons) {
    const button = el('button', { type: 'button', class: 'ghost-button', text: label });
    button.addEventListener('click', handler);
    actionRow.append(button);
  }
  container.append(actionRow);
  return container;
}

function pillList(
  ids: string[],
  name: (id: string) => string,
  actions: PanelActions,
): HTMLElement {
  const list = el('div', { class: 'pill-list' });
  for (const id of ids) {
    const pill = el('button', { type: 'button', class: 'pill', text: name(id) });
    pill.addEventListener('click', () => actions.selectEntity(id));
    list.append(pill);
  }
  return list;
}

function criticalitySeverity(criticality: string): string {
  return criticality === 'MEDIUM' ? 'MODERATE' : criticality;
}

// ------------------------------------------------------------------------------------
// Timeline
// ------------------------------------------------------------------------------------

export function renderTimeline(entries: TimelineEntry[]): HTMLElement {
  const container = el('div', { class: 'timeline' });
  if (entries.length === 0) {
    container.append(el('p', { class: 'empty-state', text: 'No activity recorded yet.' }));
    return container;
  }
  for (const entry of entries) {
    container.append(
      el(
        'div',
        { class: 'timeline__entry', 'data-severity': entry.severity },
        el('span', { class: 'timeline__time', text: utcTime(entry.at) }),
        el('span', { class: 'timeline__stage', text: entry.stage }),
        el('span', { class: 'timeline__message', text: entry.message }),
      ),
    );
  }
  return container;
}

// ------------------------------------------------------------------------------------
// Simulation
// ------------------------------------------------------------------------------------

export function renderSimulation(
  state: AppState,
  actions: PanelActions,
  onAddFailure: (entityId: string, health: string) => void,
  onReset: () => void,
): HTMLElement {
  const container = el('div', { class: 'sim' });
  const left = el('div');
  const right = el('div');

  // -- failure picker ---------------------------------------------------------------
  const select = el('select', { 'aria-label': 'Entity to fail' }) as HTMLSelectElement;
  select.append(el('option', { value: '', text: 'Choose infrastructure…' }));
  const failable = state.entities
    .filter((entity) =>
      ['CLOUD_REGION', 'KUBERNETES_CLUSTER', 'DATABASE', 'SUPPLIER', 'DATACENTER', 'MICROSERVICE'].includes(
        entity.type,
      ),
    )
    .sort((a, b) => a.name.localeCompare(b.name));
  for (const entity of failable) {
    select.append(el('option', { value: entity.id, text: entity.name }));
  }

  const healthSelect = el('select', { 'aria-label': 'Failure mode' }) as HTMLSelectElement;
  for (const [value, label] of [
    ['DOWN', 'is DOWN'],
    ['SEVERELY_DEGRADED', 'is severely degraded'],
    ['DEGRADED', 'is degraded'],
  ] as const) {
    healthSelect.append(el('option', { value, text: label }));
  }

  const addButton = el('button', { type: 'button', class: 'primary-button', text: 'Add failure' });
  addButton.addEventListener('click', () => {
    if (select.value) onAddFailure(select.value, healthSelect.value);
  });

  left.append(el('div', { class: 'failure-picker' }, select, healthSelect, addButton));

  // -- active overrides ---------------------------------------------------------------
  if (state.scenario && state.scenario.overrides.length > 0) {
    const list = el('div', { class: 'override-list' });
    for (const override of state.scenario.overrides) {
      const label =
        override.kind === 'ENTITY_HEALTH'
          ? `${state.entitiesById.get(override.target_id)?.name ?? override.target_id} = ${override.health}`
          : `${override.target_id} capacity = ${roundPercent(override.capacity ?? 0)}`;
      const remove = el('button', { type: 'button', class: 'override__remove', text: '✕', 'aria-label': `Remove ${label}` });
      remove.addEventListener('click', () => actions.removeOverride(override.id));
      list.append(el('div', { class: 'override' }, el('span', { text: label }), remove));
    }
    const reset = el('button', { type: 'button', class: 'ghost-button', text: 'Reset scenario' });
    reset.addEventListener('click', onReset);
    left.append(list, reset);
  } else {
    left.append(
      el('p', {
        class: 'empty-state',
        text: 'Add a hypothetical failure above, or ask the analyst "What happens if Singapore goes offline?". The real world state is never modified.',
      }),
    );
  }

  // -- comparison ---------------------------------------------------------------------
  if (state.comparison) {
    right.append(renderComparison(state.comparison));
  } else {
    right.append(el('p', { class: 'empty-state', text: 'No scenario running.' }));
  }

  container.append(left, right);
  return container;
}

export function renderComparison(comparison: SimulationComparison): HTMLElement {
  const table = el('table', { class: 'compare' });
  table.append(
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        el('th', { text: 'Metric' }),
        el('th', { text: 'Current' }),
        el('th', { text: 'Simulation' }),
      ),
    ),
  );
  const body = el('tbody');
  for (const row of comparison.deltas) {
    body.append(
      el(
        'tr',
        { 'data-direction': row.direction },
        el('td', { text: row.label }),
        el('td', { text: row.baseline }),
        el('td', { class: 'compare__sim', text: row.simulated }),
      ),
    );
  }
  table.append(body);

  const container = el('div', {}, table);
  if (comparison.cascade_paths.length > 0) {
    const paths = el('div');
    for (const path of comparison.cascade_paths.slice(0, 3)) paths.append(pathChain(path.hops));
    container.append(section('Cascading failure paths', paths));
  }
  container.append(disclaimer('Simulated — modelled estimate over a hypothetical world state'));
  return container;
}

// ------------------------------------------------------------------------------------
// Response plan
// ------------------------------------------------------------------------------------

export function renderPlan(plan: ResponsePlan): HTMLElement {
  const container = el('div');
  container.append(el('p', { class: 'row__summary', style: '-webkit-line-clamp:6', text: plan.summary }));

  for (const [index, action] of plan.actions.entries()) {
    container.append(
      el(
        'div',
        { class: 'plan__action' },
        el(
          'div',
          { class: 'plan__action-head' },
          el('span', { class: 'plan__index', text: `${index + 1}.` }),
          el('span', { class: 'plan__action-text', text: action.action }),
          badge(action.urgency, action.urgency === 'NOW' ? 'HIGH' : action.urgency === 'SOON' ? 'MODERATE' : 'INFO'),
        ),
        el('p', { class: 'plan__rationale', text: action.rationale }),
        el('div', {
          class: 'plan__footnote',
          text: `Confidence ${Math.round(action.confidence * 100)}%${action.requires_approval ? ' · requires human approval' : ''} · not executed`,
        }),
      ),
    );
  }

  if (plan.assumptions.length > 0) {
    container.append(
      section(
        'Assumptions',
        el('ul', { class: 'evidence' }, ...plan.assumptions.map((item) => el('li', { 'data-marker': '·', text: item }))),
      ),
    );
  }
  if (plan.unresolved_questions.length > 0) {
    container.append(
      section(
        'Open questions',
        el(
          'ul',
          { class: 'evidence' },
          ...plan.unresolved_questions.slice(0, 6).map((item) => el('li', { 'data-kind': 'uncertain', 'data-marker': '?', text: item })),
        ),
      ),
    );
  }
  container.append(disclaimer('Recommendations only — WorldGraph executes nothing'));
  return container;
}

// ------------------------------------------------------------------------------------
// Dependencies dock
// ------------------------------------------------------------------------------------

export function renderDependencies(state: AppState, actions: PanelActions): HTMLElement {
  const container = el('div');
  const entity = state.selectedEntity?.entity;
  if (!entity) {
    container.append(
      el('p', { class: 'empty-state', text: 'Select an entity to inspect its dependency graph.' }),
    );
    return container;
  }
  const detail = state.selectedEntity!;
  const name = (id: string) => state.entitiesById.get(id)?.name ?? id;

  const grid = el('div', { class: 'sim' });
  grid.append(
    el(
      'div',
      {},
      el('h3', { class: 'detail__section-title', text: `${entity.name} depends on` }),
      detail.depends_on.length
        ? edgeTable(detail.depends_on, (edge) => name(edge.target_entity_id))
        : el('p', { class: 'empty-state', text: 'No dependencies.' }),
    ),
    el(
      'div',
      {},
      el('h3', { class: 'detail__section-title', text: `Depends on ${entity.name}` }),
      detail.dependents.length
        ? edgeTable(detail.dependents, (edge) => name(edge.source_entity_id))
        : el('p', { class: 'empty-state', text: 'No dependents.' }),
    ),
  );
  container.append(grid);

  const trace = el('button', { type: 'button', class: 'ghost-button', text: 'Trace on globe' });
  trace.addEventListener('click', () => actions.traceDependencies(entity.id));
  container.append(el('div', { class: 'detail__actions', style: 'margin-top:12px' }, trace));
  return container;
}

function edgeTable(
  edges: EntityDetail['depends_on'],
  label: (edge: EntityDetail['depends_on'][number]) => string,
): HTMLElement {
  const table = el('table', { class: 'compare' });
  table.append(
    el(
      'thead',
      {},
      el(
        'tr',
        {},
        el('th', { text: 'Entity' }),
        el('th', { text: 'Type' }),
        el('th', { text: 'Criticality' }),
        el('th', { text: 'Redundancy' }),
      ),
    ),
  );
  const body = el('tbody');
  for (const edge of edges) {
    body.append(
      el(
        'tr',
        {},
        el('td', { text: label(edge) }),
        el('td', { text: humanize(edge.type) }),
        el('td', { text: edge.criticality.toFixed(2) }),
        el('td', { text: edge.redundancy.toFixed(2) }),
      ),
    );
  }
  table.append(body);
  return table;
}

// ------------------------------------------------------------------------------------
// Event card helper used by the globe overlay
// ------------------------------------------------------------------------------------

export function eventSummaryLine(event: WorldEvent): string {
  return `${event.severity} · ${humanize(event.category)} · ${relativeTime(event.occurred_at)}`;
}
