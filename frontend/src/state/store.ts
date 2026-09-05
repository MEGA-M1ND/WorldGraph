/**
 * Application state.
 *
 * A tiny observable store rather than a framework. The state here is genuinely small — a
 * selection, a mode, an analysis, a scenario — and the baseline audit's ADR-5 argued
 * against importing a UI framework to manage it.
 *
 * Two rules the panels rely on:
 *
 * * Every mutation goes through `update`, so nothing changes without a notification.
 * * Subscribers receive the whole state; they decide what changed. With this few keys,
 *   fine-grained selectors would cost more than the re-renders they save.
 */

import type {
  AppConfig,
  BlastRadiusResult,
  Dashboard,
  DependencyEdge,
  EntityDetail,
  EventDetail,
  FeedStatus,
  MaterialRisk,
  ReplayScenario,
  ResponsePlan,
  SimulationComparison,
  SimulationScenario,
  TimelineEntry,
  WorldEntity,
  WorldEvent,
  WorldSnapshotMetrics,
} from '../types.ts';

/** A line in the analyst transcript. */
export interface AnalystMessage {
  role: 'operator' | 'analyst' | 'system';
  text: string;
  at: number;
  engine?: 'deterministic' | 'model';
  tools?: string[];
  degradedReason?: string | null;
}

/** A user-visible problem, shown as a specific banner rather than a generic error. */
export interface Notice {
  id: string;
  tone: 'error' | 'warning' | 'info';
  title: string;
  detail: string;
}

export type ViewMode = 'executive' | 'engineer';

export interface AppState {
  config: AppConfig | null;
  dashboard: Dashboard | null;
  metrics: WorldSnapshotMetrics | null;

  entities: WorldEntity[];
  entitiesById: Map<string, WorldEntity>;
  edges: DependencyEdge[];
  events: WorldEvent[];
  feeds: FeedStatus[];
  risks: MaterialRisk[];
  timeline: TimelineEntry[];
  replayScenarios: ReplayScenario[];

  selectedEntityId: string | null;
  selectedEntity: EntityDetail | null;
  selectedEventId: string | null;
  selectedEvent: EventDetail | null;

  analysis: BlastRadiusResult | null;
  plan: ResponsePlan | null;

  scenario: SimulationScenario | null;
  comparison: SimulationComparison | null;
  simulationActive: boolean;

  /** Entity ids currently highlighted on the globe. */
  highlightedEntityIds: string[];
  /** An entity-id chain rendered as a lit dependency path. */
  focusedPath: string[];
  showDependencies: boolean;

  viewMode: ViewMode;
  transcript: AnalystMessage[];
  notices: Notice[];
  /** Named in-flight operations, so the UI can show precise progress rather than a spinner. */
  busy: Set<string>;
  firstRunDismissed: boolean;
}

export type Listener = (state: AppState) => void;

function initialState(): AppState {
  return {
    config: null,
    dashboard: null,
    metrics: null,
    entities: [],
    entitiesById: new Map(),
    edges: [],
    events: [],
    feeds: [],
    risks: [],
    timeline: [],
    replayScenarios: [],
    selectedEntityId: null,
    selectedEntity: null,
    selectedEventId: null,
    selectedEvent: null,
    analysis: null,
    plan: null,
    scenario: null,
    comparison: null,
    simulationActive: false,
    highlightedEntityIds: [],
    focusedPath: [],
    showDependencies: true,
    viewMode: 'executive',
    transcript: [],
    notices: [],
    busy: new Set(),
    firstRunDismissed: false,
  };
}

export class Store {
  private state: AppState = initialState();
  private listeners = new Set<Listener>();

  get(): Readonly<AppState> {
    return this.state;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => this.listeners.delete(listener);
  }

  update(patch: Partial<AppState>): void {
    this.state = { ...this.state, ...patch };
    // Derived index, rebuilt whenever entities change so no caller has to remember to.
    if (patch.entities) {
      this.state.entitiesById = new Map(patch.entities.map((entity) => [entity.id, entity]));
    }
    for (const listener of this.listeners) listener(this.state);
  }

  entity(id: string | null | undefined): WorldEntity | undefined {
    return id ? this.state.entitiesById.get(id) : undefined;
  }

  entityName(id: string): string {
    return this.state.entitiesById.get(id)?.name ?? id;
  }

  event(id: string | null | undefined): WorldEvent | undefined {
    return id ? this.state.events.find((event) => event.id === id) : undefined;
  }

  // -- transcript --------------------------------------------------------------------

  appendMessage(message: AnalystMessage): void {
    this.update({ transcript: [...this.state.transcript, message] });
  }

  // -- notices -----------------------------------------------------------------------

  /**
   * Raise a specific, actionable notice.
   *
   * Deduplicated by id so a repeatedly failing poll does not stack a hundred identical
   * banners — the operator needs to know a thing is broken once.
   */
  notify(notice: Notice): void {
    const existing = this.state.notices.filter((item) => item.id !== notice.id);
    this.update({ notices: [...existing, notice] });
  }

  dismissNotice(id: string): void {
    this.update({ notices: this.state.notices.filter((notice) => notice.id !== id) });
  }

  // -- busy tracking -----------------------------------------------------------------

  /** Mark an operation in flight. Returns a function that clears it. */
  startWork(label: string): () => void {
    const busy = new Set(this.state.busy);
    busy.add(label);
    this.update({ busy });
    return () => {
      const next = new Set(this.state.busy);
      next.delete(label);
      this.update({ busy: next });
    };
  }

  isBusy(label?: string): boolean {
    return label ? this.state.busy.has(label) : this.state.busy.size > 0;
  }
}

export const store = new Store();
