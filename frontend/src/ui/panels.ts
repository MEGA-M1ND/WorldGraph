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
  WorkspaceDetail,
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
  const risk = live?.material_risk ?? 'LOW';

  // Two different availabilities, and which one is shown says something. The
  // customer-experienced figure exists only for an estate that declares customers and
  // traffic; an imported cloud estate does not, so the bar falls back to the
  // infrastructure figure and *relabels itself* rather than printing an infrastructure
  // number under a customer heading. (docs/REALITY_PASS_AUDIT.md, C1.)
  const customerAvailability = live ? live.availability : dashboard.availability;
  const infrastructure = live
    ? live.infrastructure_availability
    : dashboard.infrastructure_availability;
  const availabilityStat: [string, string] =
    customerAvailability === null
      ? ['Infra availability', percent(infrastructure)]
      : ['Availability', percent(customerAvailability)];

  const stats: [string, string, string?][] = [
    ['Critical services', count(dashboard.critical_services)],
    ['Infrastructure', count(dashboard.infrastructure_assets)],
    ['Active incidents', count(dashboard.active_incidents)],
    ['Material risks', count(dashboard.material_risks)],
    availabilityStat,
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
// Graph coverage
// ------------------------------------------------------------------------------------

/**
 * What WorldGraph knows about the current estate, dimension by dimension.
 *
 * Never collapsed into one confidence percentage. Averaging "complete hosting topology"
 * with "nothing at all about business services" produces a number that looks precise and
 * carries no information; the operator needs the *shape* of the gap, because that is what
 * tells them which tag to add.
 *
 * Renders nothing for a workspace that was not imported — the demo fixture declares
 * everything by construction, so a coverage report there would be decoration.
 */
export function renderCoverage(detail: WorkspaceDetail | null): HTMLElement | null {
  const summary = detail?.import_summary;
  if (!detail || !summary) return null;

  const container = el('div');

  // Completeness first, and loudly when it is absent. Every ratio below describes what
  // was retrieved, and a ratio over a fraction of an estate says nothing about the estate:
  // a subscription of 5,000 resources that imported 1,000 once reported "7 of 7 resources
  // placed in a region — HIGH".
  if (!summary.collection_complete) {
    container.append(
      el(
        'div',
        { class: 'coverage__warning' },
        el('p', { class: 'coverage__warning-head', text: 'INCOMPLETE COLLECTION' }),
        el('p', {
          class: 'coverage__warning-detail',
          text:
            summary.truncation_reason ||
            'Not every resource the source holds was retrieved.',
        }),
        el('p', {
          class: 'coverage__warning-detail',
          text: 'Everything below describes only what was retrieved.',
        }),
      ),
    );
  }

  container.append(
    keyValue([
      ['Source', humanize(summary.source)],
      ['Resources read', count(summary.resources_discovered)],
      ...(summary.resources_reported_by_source !== null
        ? ([['Source reports', count(summary.resources_reported_by_source)]] as [
            string,
            string,
          ][])
        : []),
      ['Modelled', count(summary.resources_supported)],
      ['Not modelled', count(summary.resources_unsupported)],
      ['Entities', count(summary.entities_created)],
      // Split deliberately: an edge Azure proved and an edge a human asserted are
      // different kinds of claim and must not be added together into one total.
      ['Edges from inventory', count(summary.explicit_edges)],
      ['Edges from tags', count(summary.declared_edges)],
      ['Regions', count(summary.regions)],
    ]),
  );

  const dimensions = el('div', { class: 'coverage' });
  for (const dimension of summary.coverage) {
    dimensions.append(
      el(
        'div',
        { class: 'coverage__row', 'data-level': dimension.level },
        el(
          'div',
          { class: 'coverage__head' },
          el('span', { class: 'coverage__label', text: dimension.label }),
          badge(dimension.level, dimension.level),
        ),
        el('p', { class: 'coverage__detail', text: dimension.detail }),
        ...(dimension.remedy
          ? [el('p', { class: 'coverage__remedy', text: dimension.remedy })]
          : []),
      ),
    );
  }
  container.append(section('What WorldGraph knows', dimensions));

  if (Object.keys(summary.unsupported_types).length > 0) {
    // Reporting these is not an apology. It is the product telling the operator where the
    // edge of the graph is, so they do not read it as complete.
    container.append(
      section(
        'Resource types not modelled',
        keyValue(
          Object.entries(summary.unsupported_types)
            .slice(0, 12)
            .map(([type, n]) => [type, count(n)] as [string, string]),
        ),
      ),
    );
  }

  if (summary.rejected_tags.length > 0) {
    const list = el('ul', { class: 'unknowns__list' });
    for (const rejection of summary.rejected_tags.slice(0, 20)) {
      list.append(el('li', { text: rejection }));
    }
    container.append(
      section(
        `Tags rejected (${summary.rejected_tags.length})`,
        list,
        disclaimer(
          'WorldGraph never guesses what a malformed tag meant. These values were read, ' +
            'rejected and reported.',
        ),
      ),
    );
  }

  container.append(disclaimer(detail.disclaimer));
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

/** Copy for each assessment state. Ordered by how much the operator must not misread it. */
const ASSESSMENT_COPY: Record<
  NonNullable<BlastRadiusResult['assessment']>,
  { label: string; tone: string; detail: string }
> = {
  CONFIRMED_AFFECTED: {
    label: 'CONFIRMED AFFECTED',
    tone: 'CRITICAL',
    detail: "An asset's own software inventory names this CVE.",
  },
  POTENTIALLY_AFFECTED: {
    label: 'POTENTIALLY AFFECTED',
    tone: 'MODERATE',
    detail:
      'Matched on product name only — version and vendor were not compared. A candidate ' +
      'for triage, not a confirmed exposure.',
  },
  NOT_AFFECTED: {
    label: 'NOT AFFECTED',
    tone: 'LOW',
    detail: 'Adequate software inventory was searched and nothing matched.',
  },
  INSUFFICIENT_DATA: {
    label: 'INSUFFICIENT DATA',
    tone: 'HIGH',
    detail:
      'No conclusion was reached. WorldGraph does not have enough software inventory to ' +
      'say whether this estate is affected — this is not a finding that it is safe.',
  },
};

/**
 * The security verdict, rendered before anything else.
 *
 * Deliberately not colour-coded by severity: an INSUFFICIENT_DATA result carries
 * severity LOW and score 0, because there is no measured impact — and painting that green
 * is exactly the misreading this panel exists to prevent.
 */
function renderAssessment(analysis: BlastRadiusResult): HTMLElement {
  const assessment = analysis.assessment;
  if (assessment === null) return el('div');
  const copy = ASSESSMENT_COPY[assessment];

  const rows: (Node | string)[] = [
    el(
      'div',
      { class: 'detail__badges', style: 'margin-bottom:8px' },
      badge(copy.label, copy.tone),
    ),
    el('p', { class: 'assessment__detail', text: copy.detail }),
  ];

  const coverage = analysis.inventory_coverage;
  if (coverage !== null) {
    rows.push(
      el('p', {
        class: 'assessment__coverage',
        text:
          `Inventory searched: ${coverage.entities_with_inventory} of ` +
          `${coverage.assessable_entities} assets that could run software.`,
      }),
    );
  }

  return section('Vulnerability assessment', el('div', { class: 'assessment' }, ...rows));
}

export function renderAnalysis(
  analysis: BlastRadiusResult,
  actions: PanelActions,
): HTMLElement {
  const container = el('div');
  const impact = analysis.business_impact;

  // A security analysis leads with what could be concluded, not with a severity colour.
  // INSUFFICIENT_DATA scores 0/100 and would otherwise render as a calm green LOW — a
  // confident all-clear derived from having searched nothing.
  if (analysis.assessment !== null) {
    container.append(renderAssessment(analysis));
  }

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

  // Rows that need no business metadata come first, because for an imported estate they
  // are the only ones that will carry a number. The customer rows are omitted entirely
  // rather than printed as UNKNOWN when nothing about the estate could ever fill them —
  // a wall of UNKNOWN teaches the operator to stop reading the panel.
  const impactRows: [string, string][] = [
    ['Infra availability', percent(impact.infrastructure_availability)],
    ['Entities impacted', count(impact.impacted_entity_count)],
    ['Critical services', count(impact.critical_services_impacted)],
    ['SLA breaches', count(impact.sla_breaches.length)],
  ];
  if (impact.availability !== null) {
    impactRows.unshift(['Customer availability', percent(impact.availability)]);
  }
  if (impact.traffic_impact !== null) {
    impactRows.push(['Traffic impacted', roundPercent(impact.traffic_impact)]);
  }
  if (impact.customers_affected !== null) {
    impactRows.push(['Customers', count(impact.customers_affected)]);
  }
  if (impact.revenue_at_risk_per_hour !== null) {
    impactRows.push(['Revenue at risk', `${money(impact.revenue_at_risk_per_hour)}/hr`]);
  }

  const impactSection = section(
    'Business impact',
    keyValue(impactRows),
    disclaimer(`${impact.disclaimer} — WorldGraph V1 Impact Model`),
  );
  if (impact.unknown_reasons.length > 0) {
    // Naming the missing input turns a gap into something the operator can close.
    impactSection.append(
      el(
        'div',
        { class: 'unknowns' },
        el('p', { class: 'unknowns__head', text: 'Not computed, and why' }),
        el(
          'ul',
          { class: 'unknowns__list' },
          ...impact.unknown_reasons.map((reason) => el('li', { text: reason })),
        ),
      ),
    );
  }
  container.append(impactSection);

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
