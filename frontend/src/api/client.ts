/**
 * Backend client.
 *
 * Three responsibilities beyond fetching:
 *
 * 1. **Request cancellation.** Selecting entities quickly must not leave stale responses
 *    racing to overwrite the panel. Every keyed request aborts its predecessor.
 * 2. **Honest errors.** The backend returns a specific `detail` for every failure; this
 *    surfaces it verbatim rather than replacing it with "something went wrong".
 * 3. **Workspace scoping.** Every request carries the active workspace, set once here
 *    rather than threaded through a hundred call sites. Forgetting it on one endpoint
 *    would show one estate's data inside another's, so it must not be per-call.
 * 4. **Nothing else.** No caching layer, no retry policy, no state beyond that scope.
 *    The backend owns freshness and the store owns state.
 */

import type {
  AppConfig,
  AskResponse,
  AttackPathResponse,
  BlastRadiusResult,
  Dashboard,
  EntityDetail,
  EventDetail,
  FeedStatus,
  MaterialRisk,
  ReplayScenario,
  ResponsePlan,
  SimulationComparison,
  SimulationScenario,
  TimelineEntry,
  WorkspaceDetail,
  WorkspaceListResponse,
  ImportSummary,
  WorldEvent,
  WorldResponse,
} from '../types.ts';

const API_BASE = '/api';

/** In-flight requests keyed by caller-supplied slot, so a newer one cancels an older one. */
const inFlight = new Map<string, AbortController>();

/**
 * The estate every subsequent request is about.
 *
 * `null` means the backend's default. Held here, in one place, because a request that
 * forgot the workspace would silently answer with the wrong estate's data — and the whole
 * point of workspaces is that this cannot happen.
 */
let activeWorkspace: string | null = null;

export function setActiveWorkspace(workspaceId: string | null): void {
  activeWorkspace = workspaceId;
  // Anything still in flight was asked about the previous estate. Cancel it rather than
  // letting a late response paint the new workspace's panels with old data.
  for (const controller of inFlight.values()) controller.abort();
  inFlight.clear();
}

export function getActiveWorkspace(): string | null {
  return activeWorkspace;
}

/** Append the active workspace to a path, preserving any query string it already has. */
function scoped(path: string): string {
  if (!activeWorkspace) return path;
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}workspace=${encodeURIComponent(activeWorkspace)}`;
}

/** A failure the UI can explain to the operator, carrying the backend's own message. */
export class ApiError extends Error {
  readonly status: number;
  readonly path: string;

  constructor(message: string, status: number, path: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.path = path;
  }

  /** Whether this looks like the backend being down rather than rejecting a request. */
  get isUnreachable(): boolean {
    return this.status === 0 || this.status >= 502;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE';
  body?: unknown;
  /** Cancellation slot. Two requests with the same key never overlap. */
  key?: string;
  timeoutMs?: number;
  /** Skip workspace scoping. Only for endpoints that are about the deployment itself. */
  unscoped?: boolean;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, key, timeoutMs = 20_000 } = options;

  if (key) {
    inFlight.get(key)?.abort();
  }
  const controller = new AbortController();
  if (key) inFlight.set(key, controller);
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(`${API_BASE}${options.unscoped ? path : scoped(path)}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new ApiError(await readErrorDetail(response, path), response.status, path);
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === 'AbortError') {
      // A superseded request is not a failure — it is the cancellation working.
      throw new ApiError('Request superseded.', 499, path);
    }
    throw new ApiError('WorldGraph API is unreachable. Is the backend running?', 0, path);
  } finally {
    clearTimeout(timer);
    if (key && inFlight.get(key) === controller) inFlight.delete(key);
  }
}

/** Pull the backend's specific message out of a failed response. */
async function readErrorDetail(response: Response, path: string): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === 'string') return payload.detail;
    if (Array.isArray(payload.detail)) {
      // FastAPI validation errors arrive as a list of location/message objects.
      const first = payload.detail[0] as { loc?: unknown[]; msg?: string } | undefined;
      if (first?.msg) return `Invalid request: ${first.msg}`;
    }
  } catch {
    // Fall through to the status-based message below.
  }
  return `${path} failed with HTTP ${response.status}.`;
}

/** True when a rejection is a superseded request rather than a real failure. */
export function isSuperseded(error: unknown): boolean {
  return error instanceof ApiError && error.status === 499;
}

export const api = {
  config: () => request<AppConfig>('/config', { unscoped: true }),

  workspaces: () => request<WorkspaceListResponse>('/workspaces', { unscoped: true }),
  currentWorkspace: () => request<WorkspaceDetail>('/workspaces/current', { key: 'workspace' }),
  importWorkspace: (id: string) =>
    request<ImportSummary>(`/workspaces/${encodeURIComponent(id)}/import`, {
      method: 'POST',
      unscoped: true,
      // An import reads a whole subscription. It is the one call that legitimately takes
      // longer than an interaction.
      timeoutMs: 120_000,
    }),

  dashboard: () => request<Dashboard>('/dashboard', { key: 'dashboard' }),
  feeds: () => request<FeedStatus[]>('/feeds', { key: 'feeds' }),
  refreshFeeds: () => request<FeedStatus[]>('/feeds/refresh', { method: 'POST' }),
  timeline: (limit = 60) => request<TimelineEntry[]>(`/timeline?limit=${limit}`, { key: 'timeline' }),

  world: () => request<WorldResponse>('/world', { key: 'world' }),
  entity: (id: string) =>
    request<EntityDetail>(`/world/entities/${encodeURIComponent(id)}`, { key: 'entity' }),
  risks: () => request<MaterialRisk[]>('/world/risks', { key: 'risks' }),
  trace: (id: string, direction: 'dependencies' | 'dependents', maxDepth = 4) =>
    request<{
      origin: string;
      direction: string;
      truncated: boolean;
      truncation_reason: string | null;
      cycles: string[][];
      steps: { entity_id: string; depth: number; path: string[]; edge_types: string[] }[];
    }>(
      `/world/trace/${encodeURIComponent(id)}?direction=${direction}&max_depth=${maxDepth}`,
      { key: 'trace' },
    ),

  events: (limit = 50) => request<WorldEvent[]>(`/events?limit=${limit}`, { key: 'events' }),
  event: (id: string) =>
    request<EventDetail>(`/events/${encodeURIComponent(id)}`, { key: 'event' }),
  replayScenarios: () => request<ReplayScenario[]>('/events/scenarios'),

  blastRadius: (body: { entity_ids?: string[]; event_id?: string }) =>
    request<BlastRadiusResult>('/analysis/blast-radius', {
      method: 'POST',
      body,
      key: 'blast',
    }),
  analysis: (id: string) => request<BlastRadiusResult>(`/analysis/${encodeURIComponent(id)}`),
  responsePlan: (body: { analysis_id?: string; scenario_id?: string; event_id?: string }) =>
    request<ResponsePlan>('/analysis/response-plan', { method: 'POST', body, key: 'plan' }),
  attackPaths: (toEntityId: string) =>
    request<AttackPathResponse>(
      `/analysis/security/attack-paths?to_entity_id=${encodeURIComponent(toEntityId)}`,
      { key: 'attack' },
    ),

  createScenario: (body: { name: string; description?: string; origin_event_id?: string }) =>
    request<SimulationScenario>('/simulation', { method: 'POST', body }),
  scenario: (id: string) =>
    request<SimulationScenario>(`/simulation/${encodeURIComponent(id)}`),
  addOverride: (
    scenarioId: string,
    body: { target_id: string; kind?: string; health?: string; capacity?: number; note?: string },
  ) =>
    request<SimulationScenario>(
      `/simulation/${encodeURIComponent(scenarioId)}/overrides`,
      { method: 'POST', body },
    ),
  removeOverride: (scenarioId: string, overrideId: string) =>
    request<SimulationScenario>(
      `/simulation/${encodeURIComponent(scenarioId)}/overrides/${encodeURIComponent(overrideId)}`,
      { method: 'DELETE' },
    ),
  resetScenario: (scenarioId: string) =>
    request<SimulationScenario>(`/simulation/${encodeURIComponent(scenarioId)}/reset`, {
      method: 'POST',
    }),
  compareScenario: (scenarioId: string) =>
    request<SimulationComparison>(`/simulation/${encodeURIComponent(scenarioId)}/compare`, {
      key: 'compare',
    }),

  ask: (body: {
    message: string;
    selected_entity_id?: string | null;
    selected_event_id?: string | null;
    active_scenario_id?: string | null;
  }) =>
    request<AskResponse>('/ai/ask', {
      method: 'POST',
      body: stripNulls(body),
      key: 'ask',
      timeoutMs: 60_000,
    }),
};

/** The backend forbids extra fields, and `null` for an optional id is not the same as absent. */
function stripNulls<T extends Record<string, unknown>>(body: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(body).filter(([, value]) => value !== null && value !== undefined),
  ) as Partial<T>;
}
