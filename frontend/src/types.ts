/**
 * Types mirroring the backend's Pydantic schemas.
 *
 * Hand-maintained rather than generated, because the surface is small and stable and a
 * codegen step would be more machinery than it saves at this size. The contract tests in
 * `tests/contract.test.ts` walk the live API and fail if a field drifts, so "hand-written"
 * does not mean "unverified".
 */

export type DataMode = 'LIVE' | 'REPLAY' | 'SIMULATED' | 'SYNTHETIC';

export type FeedState =
  | 'LOADING'
  | 'LIVE'
  | 'DEGRADED'
  | 'STALE'
  | 'FALLBACK'
  | 'SIMULATED'
  | 'UNAVAILABLE';

export type Severity = 'CRITICAL' | 'HIGH' | 'MODERATE' | 'LOW' | 'INFO';

/**
 * How much the business cares about an entity.
 *
 * `UNKNOWN` is the default for anything nobody has declared — an imported cloud estate
 * starts entirely UNKNOWN, and treating that as LOW would be a judgement WorldGraph has
 * no basis for.
 */
export type Criticality = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'UNKNOWN';

export type HealthState =
  | 'HEALTHY'
  | 'DEGRADED'
  | 'SEVERELY_DEGRADED'
  | 'DOWN'
  | 'UNKNOWN';

export type EntityType =
  | 'ORGANIZATION'
  | 'BUSINESS_SERVICE'
  | 'APPLICATION'
  | 'MICROSERVICE'
  | 'DATABASE'
  | 'KUBERNETES_CLUSTER'
  | 'CLOUD_REGION'
  | 'DATACENTER'
  | 'OFFICE'
  | 'SUPPLIER'
  | 'FACTORY'
  | 'CUSTOMER_REGION'
  | 'NETWORK_NODE'
  | 'EXTERNAL_API'
  | 'SECURITY_FINDING'
  | 'WORLD_EVENT';

export type DependencyType =
  | 'DEPENDS_ON'
  | 'HOSTED_IN'
  | 'CONNECTS_TO'
  | 'SUPPLIED_BY'
  | 'SERVES'
  | 'REPLICATES_TO';

export interface GeoPoint {
  lat: number;
  lon: number;
  altitude: number | null;
}

export interface DataSourceInfo {
  source_id: string;
  source_name: string;
  source_url: string | null;
  mode: DataMode;
  confidence: number;
  observed_at: string | null;
  ingested_at: string;
}

export interface BusinessProfile {
  traffic_share: number;
  revenue_per_hour: number;
  customer_count: number;
  region: string;
  sla_tier: string;
  capacity: number;
  redundancy: number;
}

export interface ExposureProfile {
  internet_facing: boolean;
  network_zone: string;
  authenticated: boolean;
}

export interface SoftwareComponent {
  name: string;
  version: string;
  vendor: string;
  cve_ids: string[];
}

export interface WorldEntity {
  id: string;
  type: EntityType;
  name: string;
  description: string;
  location: GeoPoint | null;
  health: HealthState;
  criticality: Criticality;
  /** `null` when undeclared. An imported estate declares this only through a tag. */
  customer_facing: boolean | null;
  business: BusinessProfile;
  exposure: ExposureProfile;
  software: SoftwareComponent[];
  metadata: Record<string, unknown>;
  source: DataSourceInfo;
  observed_at: string | null;
  updated_at: string;
}

export interface DependencyEdge {
  id: string;
  source_entity_id: string;
  target_entity_id: string;
  type: DependencyType;
  criticality: number;
  redundancy: number;
  capacity_impact: number | null;
  metadata: Record<string, unknown>;
}

export interface WorldEvent {
  id: string;
  category: string;
  title: string;
  /** External text. Rendered as text only, never as markup. */
  description: string;
  severity: Severity;
  location: GeoPoint | null;
  exposure_radius_km: number;
  occurred_at: string;
  source: DataSourceInfo;
  metadata: Record<string, unknown>;
  directly_named_entity_ids: string[];
  updated_at: string;
}

export interface FeedStatus {
  adapter_id: string;
  adapter_name: string;
  state: FeedState;
  mode: DataMode;
  record_count: number;
  last_success_at: string | null;
  last_attempt_at: string | null;
  message: string | null;
  source_url: string | null;
}

/**
 * Headline numbers for one world state.
 *
 * The nullable fields are the ones an estate has to *declare* before WorldGraph can
 * compute them. `null` means unknown and must render as UNKNOWN — never as 0, 100 % or a
 * dash that reads like a zero. `infrastructure_availability` needs only the graph, so it
 * is always a number and is what an imported cloud estate can actually show.
 */
export interface WorldSnapshotMetrics {
  availability: number | null;
  infrastructure_availability: number;
  regional_capacity: Record<string, number>;
  critical_services_impacted: number;
  customer_regions_impacted: number;
  customers_affected: number | null;
  revenue_at_risk_per_hour: number | null;
  material_risk: Severity;
  /** One line per figure WorldGraph could not compute, naming the missing input. */
  unknown_reasons: string[];
  disclaimer: 'MODELLED ESTIMATE';
}

export interface ScoreContribution {
  code: string;
  label: string;
  points: number;
  detail: string;
}

export interface PathHop {
  entity_id: string;
  entity_name: string;
  edge_type: DependencyType | null;
  availability: number;
}

export interface ImpactPath {
  hops: PathHop[];
  terminal_availability: number;
}

export interface ImpactedEntity {
  entity_id: string;
  entity_name: string;
  entity_type: string;
  criticality: Criticality;
  depth: number;
  availability: number;
  availability_delta: number;
  projected_health: HealthState;
  path: ImpactPath;
  /** `null` when nothing declares whether customers can see this. Not the same as false. */
  customer_facing: boolean | null;
}

export interface CustomerExposure {
  entity_id: string;
  region: string;
  customer_count: number;
  traffic_impact: number;
  projected_availability: number;
  via_service_ids: string[];
}

export interface BusinessImpact {
  /** Availability as customers experience it. `null` when nothing declares customers. */
  availability: number | null;
  traffic_impact: number | null;
  customers_affected: number | null;
  revenue_at_risk_per_hour: number | null;
  /** Availability of the infrastructure itself. Always computable from the graph alone. */
  infrastructure_availability: number;
  sla_breaches: string[];
  critical_services_impacted: number;
  customer_regions_impacted: number;
  impacted_entity_count: number;
  unknown_reasons: string[];
  disclaimer: 'MODELLED ESTIMATE';
}

/**
 * What WorldGraph could conclude about a vulnerability.
 *
 * `INSUFFICIENT_DATA` is not a softer `NOT_AFFECTED`. It means no conclusion was reached
 * because there was not enough inventory to reach one, and it must never be rendered as
 * reassurance.
 */
export type VulnerabilityAssessment =
  | 'CONFIRMED_AFFECTED'
  | 'POTENTIALLY_AFFECTED'
  | 'NOT_AFFECTED'
  | 'INSUFFICIENT_DATA';

export interface InventoryCoverage {
  assessable_entities: number;
  entities_with_inventory: number;
}

export interface Confidence {
  score: number;
  strong_evidence: string[];
  uncertainties: string[];
}

export interface RiskScore {
  score: number;
  severity: Severity;
  contributions: ScoreContribution[];
}

export interface BlastRadiusResult {
  id: string;
  origin_kind: 'EVENT' | 'ENTITY' | 'SCENARIO' | 'SECURITY_FINDING';
  origin_ids: string[];
  origin_label: string;
  severity: Severity;
  risk: RiskScore;
  direct_impact: ImpactedEntity[];
  indirect_impact: ImpactedEntity[];
  critical_paths: ImpactPath[];
  customer_exposure: CustomerExposure[];
  business_impact: BusinessImpact;
  explanations: string[];
  confidence: Confidence;
  /** Present for security analyses only. */
  assessment: VulnerabilityAssessment | null;
  inventory_coverage: InventoryCoverage | null;
  truncated: boolean;
  truncation_reason: string | null;
  cycles_detected: string[][];
  mode: DataMode;
  computed_at: string;
  duration_ms: number;
}

export type OverrideKind = 'ENTITY_HEALTH' | 'ENTITY_CAPACITY' | 'EDGE_DISABLED';

export interface SimulationOverride {
  id: string;
  kind: OverrideKind;
  target_id: string;
  health: HealthState | null;
  capacity: number | null;
  note: string;
}

export interface SimulationScenario {
  id: string;
  name: string;
  description: string;
  overrides: SimulationOverride[];
  origin_event_id: string | null;
  created_at: string;
  updated_at: string;
  mode: 'SIMULATED';
}

export interface MetricDelta {
  key: string;
  label: string;
  baseline: string;
  simulated: string;
  direction: 'worse' | 'better' | 'same';
}

export interface SimulationComparison {
  scenario: SimulationScenario;
  baseline: WorldSnapshotMetrics;
  simulated: WorldSnapshotMetrics;
  deltas: MetricDelta[];
  newly_impacted: ImpactedEntity[];
  cascade_paths: ImpactPath[];
  blast_radius: BlastRadiusResult | null;
  computed_at: string;
  duration_ms: number;
}

export type Urgency = 'NOW' | 'SOON' | 'MONITOR';

export interface ResponseAction {
  action: string;
  rationale: string;
  affected_entities: string[];
  urgency: Urgency;
  confidence: number;
  requires_approval: boolean;
  executed: false;
}

export interface ResponsePlan {
  id: string;
  summary: string;
  objectives: string[];
  actions: ResponseAction[];
  assumptions: string[];
  unresolved_questions: string[];
  generated_at: string;
  generator: 'deterministic' | 'llm-assisted';
}

export interface TimelineEntry {
  id: string;
  at: string;
  stage: string;
  message: string;
  severity: Severity;
  entity_ids: string[];
  event_id: string | null;
  metadata: Record<string, unknown>;
}

export interface MaterialRisk {
  id: string;
  title: string;
  severity: Severity;
  summary: string;
  focus_entity_ids: string[];
  contributions: ScoreContribution[];
  score: number;
}

export interface Dashboard {
  workspace_id: string;
  workspace_name: string;
  workspace_kind: WorkspaceKind;
  read_only: boolean;
  organization: string;
  critical_services: number;
  infrastructure_assets: number;
  active_incidents: number;
  material_risks: number;
  /** Customer-experienced availability. `null` when the estate declares no customers. */
  availability: number | null;
  /** Availability of the infrastructure itself. Always computable from the graph. */
  infrastructure_availability: number;
  unknown_reasons: string[];
  entities: number;
  edges: number;
  events: number;
  mode: 'LIVE' | 'DEMO' | 'OFFLINE';
  data_disclaimer: string;
}

export type WorkspaceKind = 'DEMO' | 'REAL';
export type InventorySourceKind = 'FIXTURE' | 'AZURE' | 'SNAPSHOT';
export type WorkspaceStatus = 'READY' | 'NOT_LOADED' | 'UNAVAILABLE' | 'LOADING';

/** One isolated estate. Its own graph, its own events, its own everything. */
export interface Workspace {
  id: string;
  name: string;
  kind: WorkspaceKind;
  source: InventorySourceKind;
  status: WorkspaceStatus;
  mode: DataMode;
  organization: string;
  description: string;
  /** Why a workspace could not be loaded. Specific, never "unavailable". */
  message: string | null;
  read_only: boolean;
  entity_count: number;
  edge_count: number;
  last_refreshed_at: string | null;
}

export interface WorkspaceListResponse {
  workspaces: Workspace[];
  default_id: string;
}

/**
 * What WorldGraph knows about an estate, one dimension at a time.
 *
 * Deliberately not a single confidence percentage: averaging "complete hosting topology"
 * with "no business mapping" produces a number that looks precise and means nothing.
 */
export interface GraphCoverage {
  dimension: string;
  label: string;
  level: 'HIGH' | 'PARTIAL' | 'LOW' | 'NONE';
  detail: string;
  remedy: string;
}

export interface ImportSummary {
  workspace_id: string;
  source: InventorySourceKind;
  subscription_label: string;
  resources_discovered: number;
  resources_supported: number;
  resources_unsupported: number;
  unsupported_types: Record<string, number>;
  entities_created: number;
  /** Edges the source itself proved, e.g. an explicit resource reference. */
  explicit_edges: number;
  /** Edges a human declared through WorldGraph tags. */
  declared_edges: number;
  regions: number;
  /** Tag values that were rejected, with why. Never silently ignored. */
  rejected_tags: string[];
  coverage: GraphCoverage[];
  mode: DataMode;
  refreshed_at: string;
  duration_ms: number;
}

export interface WorkspaceDetail {
  workspace: Workspace;
  disclaimer: string;
  loaded: boolean;
  import_summary: ImportSummary | null;
}

export interface AnalystStatus {
  engine: 'deterministic' | 'model';
  model: string | null;
  notice: string | null;
  available: boolean;
}

export interface AppConfig {
  run_mode: 'LIVE' | 'DEMO' | 'OFFLINE';
  ai_enabled: boolean;
  ai_model: string | null;
  cesium_ion_token: string | null;
  google_maps_api_key: string | null;
  network_enabled: boolean;
  analyst: AnalystStatus;
}

export interface WorldResponse {
  entities: WorldEntity[];
  edges: DependencyEdge[];
  metrics: WorldSnapshotMetrics;
}

export interface EntityDetail {
  entity: WorldEntity;
  depends_on: DependencyEdge[];
  dependents: DependencyEdge[];
  hosts: string[];
  availability: number;
  capacity: number;
  recent_event_ids: string[];
  risk_count: number;
}

export interface AssetInRadius {
  entity_id: string;
  name: string;
  type: string;
  distance_km: number;
  direction: string;
  proximity: number;
}

export interface EventDetail {
  event: WorldEvent;
  proximity: {
    critical_facilities?: number;
    suppliers?: number;
    dependent_services?: number;
    assets_in_radius?: number;
  };
  assets_in_radius: AssetInRadius[];
  vulnerable_assets: {
    entity_id: string;
    name: string;
    component: string;
    internet_facing: boolean;
  }[];
  freshness_seconds: number | null;
}

/** A camera/highlight instruction the analyst asked the UI to carry out. */
export interface AnalystDirective {
  kind: string;
  entity_id?: string;
  event_id?: string;
  entity_ids?: string[];
  path?: string[];
  label?: string;
  layer?: string;
  visible?: boolean;
  scenario_id?: string;
  active?: boolean;
}

export interface AskResponse {
  answer: string;
  engine: 'deterministic' | 'model';
  tool_calls: { tool: string; args: Record<string, unknown>; ok: boolean; error?: string }[];
  directives: AnalystDirective[];
  degraded_reason: string | null;
  active_scenario_id: string | null;
  duration_ms: number;
}

export interface ReplayScenario {
  id: string;
  name: string;
  summary: string;
  event_id: string;
  focus_entity_ids: string[];
  suggested_overrides: { target_id: string; health: string }[];
}

export interface AttackPathResponse {
  from: string;
  to: string | null;
  count: number;
  paths: { ids: string[]; names: string[] }[];
  disclaimer: string;
}
