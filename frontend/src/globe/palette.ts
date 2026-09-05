/**
 * The globe's visual language.
 *
 * One place for every colour, size and symbol decision, so the globe and the panels cannot
 * drift apart. Deliberately restrained: severity carries colour, everything else is
 * neutral. A globe where twelve things are bright is a globe where nothing is.
 *
 * Explicitly *not* military styling — no reticles, no threat cones, no scanline green.
 * WorldGraph is enterprise operations software.
 */

import { Color } from 'cesium';
import type { Criticality, EntityType, Severity } from '../types.ts';

/** Severity ramp, shared by the globe, the event feed and the risk list. */
export const SEVERITY_COLORS: Record<Severity, string> = {
  CRITICAL: '#ff4d5e',
  HIGH: '#ff8a3d',
  MODERATE: '#ffc247',
  LOW: '#4da3ff',
  INFO: '#8b93a7',
};

/** Health ramp for impacted entities. Green is reserved for "actually fine". */
export const HEALTH_COLORS = {
  HEALTHY: '#3ddc97',
  DEGRADED: '#ffc247',
  SEVERELY_DEGRADED: '#ff8a3d',
  DOWN: '#ff4d5e',
  UNKNOWN: '#8b93a7',
} as const;

/** Neutral entity colour, before any impact is applied. */
export const ENTITY_BASE = '#6f7c96';

/** Accent used for the current selection. */
export const SELECTION = '#ffffff';

/** Dependency arcs at rest, and while lit by a propagation animation. */
export const EDGE_IDLE = 'rgba(120, 138, 173, 0.28)';
export const EDGE_ACTIVE = '#ff8a3d';
export const EDGE_CRITICAL = '#ff4d5e';

/**
 * One glyph per entity type.
 *
 * Text glyphs rather than sprite sheets: 42 entities do not justify an atlas, and a glyph
 * stays crisp at every zoom without a second asset pipeline.
 */
export const ENTITY_GLYPHS: Record<EntityType, string> = {
  ORGANIZATION: '◆',
  BUSINESS_SERVICE: '◉',
  APPLICATION: '▣',
  MICROSERVICE: '▪',
  DATABASE: '⬢',
  KUBERNETES_CLUSTER: '⬡',
  CLOUD_REGION: '▲',
  DATACENTER: '▮',
  OFFICE: '⌂',
  SUPPLIER: '⬗',
  FACTORY: '⌸',
  CUSTOMER_REGION: '◍',
  NETWORK_NODE: '⌗',
  EXTERNAL_API: '⇄',
  SECURITY_FINDING: '⚠',
  WORLD_EVENT: '✳',
};

/** Marker radius in pixels. Criticality drives size so importance reads before colour. */
export const CRITICALITY_SIZE: Record<Criticality, number> = {
  CRITICAL: 13,
  HIGH: 11,
  MEDIUM: 9,
  LOW: 7,
};

/** Entity types shown by default. Customer regions and the org node are aggregates that
 *  clutter the globe until an analysis makes them relevant. */
export const DEFAULT_VISIBLE_TYPES: ReadonlySet<EntityType> = new Set<EntityType>([
  'CLOUD_REGION',
  'DATACENTER',
  'OFFICE',
  'SUPPLIER',
  'FACTORY',
  'KUBERNETES_CLUSTER',
  'DATABASE',
  'NETWORK_NODE',
  'CUSTOMER_REGION',
  'MICROSERVICE',
  'APPLICATION',
  'BUSINESS_SERVICE',
  'EXTERNAL_API',
]);

/** Types that represent a physical place, drawn on the surface rather than lifted. */
export const PHYSICAL_TYPES: ReadonlySet<EntityType> = new Set<EntityType>([
  'CLOUD_REGION',
  'DATACENTER',
  'OFFICE',
  'SUPPLIER',
  'FACTORY',
  'NETWORK_NODE',
]);

export function cesiumColor(css: string, alpha = 1): Color {
  return Color.fromCssColorString(css).withAlpha(alpha);
}

export function severityColor(severity: Severity): string {
  return SEVERITY_COLORS[severity];
}

/**
 * Colour an entity by its modelled availability.
 *
 * Returns the neutral base above the impact threshold so an unaffected estate reads as
 * calm — colour appearing anywhere then means something actually happened.
 */
export function availabilityColor(availability: number): string {
  if (availability >= 0.999) return ENTITY_BASE;
  if (availability >= 0.85) return HEALTH_COLORS.DEGRADED;
  if (availability >= 0.5) return HEALTH_COLORS.SEVERELY_DEGRADED;
  if (availability > 0.15) return SEVERITY_COLORS.HIGH;
  return HEALTH_COLORS.DOWN;
}

/**
 * Vertical offset in metres for a logical entity.
 *
 * Several logical entities share a site's exact coordinates (a cluster, its services, its
 * database are all "Singapore"), so stacking them by tier is what stops them rendering as
 * one unreadable pile. The tiers also read as the dependency hierarchy, which is a
 * happy accident worth keeping.
 */
export function altitudeFor(type: EntityType, index = 0): number {
  const tiers: Partial<Record<EntityType, number>> = {
    BUSINESS_SERVICE: 900_000,
    APPLICATION: 700_000,
    MICROSERVICE: 500_000,
    KUBERNETES_CLUSTER: 340_000,
    DATABASE: 220_000,
    EXTERNAL_API: 420_000,
    CUSTOMER_REGION: 1_150_000,
    ORGANIZATION: 1_400_000,
  };
  const base = tiers[type];
  if (base === undefined) return 0;
  // Fan same-tier entities at one site apart so their labels do not overlap.
  return base + index * 55_000;
}
