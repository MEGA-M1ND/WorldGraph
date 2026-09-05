/**
 * The estate on the globe: entities, dependency arcs, event markers, and the animated
 * propagation that is the product's signature moment.
 *
 * Performance discipline, following the baseline audit's measured lesson:
 *
 * * Geometry is **static**. Positions, radii and colours are plain values, replaced when
 *   state changes, never `CallbackProperty`. A per-frame callback on a ground primitive
 *   re-tessellates it 60 times a second.
 * * The only exception is the propagation animation, which holds continuous rendering for
 *   its duration and releases it the moment it finishes.
 * * Entities are created once and mutated in place; a full rebuild per selection would
 *   thrash Cesium's primitive pool for no reason.
 */

import {
  ArcType,
  CallbackProperty,
  Cartesian2,
  Cartesian3,
  Color,
  ColorMaterialProperty,
  ConstantProperty,
  DistanceDisplayCondition,
  Entity,
  HeightReference,
  HorizontalOrigin,
  LabelStyle,
  NearFarScalar,
  PolylineDashMaterialProperty,
  PolylineGlowMaterialProperty,
  ScreenSpaceEventHandler,
  ScreenSpaceEventType,
  VerticalOrigin,
} from 'cesium';

import type { GlobeHandles } from './viewer.ts';
import {
  CRITICALITY_SIZE,
  DEFAULT_VISIBLE_TYPES,
  EDGE_ACTIVE,
  EDGE_CRITICAL,
  EDGE_IDLE,
  ENTITY_BASE,
  ENTITY_GLYPHS,
  PHYSICAL_TYPES,
  SELECTION,
  altitudeFor,
  availabilityColor,
  cesiumColor,
  severityColor,
} from './palette.ts';
import type {
  DependencyEdge,
  ImpactedEntity,
  WorldEntity,
  WorldEvent,
} from '../types.ts';

/** Label visibility band. Below the far distance every label would draw at once. */
const LABEL_VISIBILITY = new DistanceDisplayCondition(0, 40_000_000);

/** How long one hop of the propagation animation takes, in milliseconds. */
const HOP_DURATION_MS = 520;

export interface EntityLayerOptions {
  globe: GlobeHandles;
  onSelectEntity: (entityId: string | null) => void;
  onSelectEvent: (eventId: string) => void;
}

interface EntityRecord {
  entity: WorldEntity;
  marker: Entity;
  /** Availability last applied, so a no-op update can skip the write. */
  availability: number;
}

export class EntityLayer {
  private readonly globe: GlobeHandles;
  private readonly onSelectEntity: (entityId: string | null) => void;
  private readonly onSelectEvent: (eventId: string) => void;

  private readonly records = new Map<string, EntityRecord>();
  private readonly edgeEntities = new Map<string, Entity>();
  private readonly eventEntities = new Map<string, Entity>();
  private readonly pathEntities: Entity[] = [];

  private handler: ScreenSpaceEventHandler | null = null;
  private selectedEntityId: string | null = null;
  private highlighted = new Set<string>();
  private edgesVisible = true;
  private animationToken = 0;

  constructor(options: EntityLayerOptions) {
    this.globe = options.globe;
    this.onSelectEntity = options.onSelectEntity;
    this.onSelectEvent = options.onSelectEvent;
    this.installPicking();
  }

  // -- picking -------------------------------------------------------------------------

  private installPicking(): void {
    const { viewer } = this.globe;
    this.handler = new ScreenSpaceEventHandler(viewer.scene.canvas);
    this.handler.setInputAction((movement: { position: Cartesian2 }) => {
      const picked = viewer.scene.pick(movement.position);
      const id = picked?.id;
      if (id instanceof Entity && typeof id.id === 'string') {
        // Ids are namespaced so one pick handler can serve three kinds of object.
        if (id.id.startsWith('event:')) {
          this.onSelectEvent(id.id.slice('event:'.length));
          return;
        }
        if (id.id.startsWith('entity:')) {
          this.onSelectEntity(id.id.slice('entity:'.length));
          return;
        }
      }
      this.onSelectEntity(null);
    }, ScreenSpaceEventType.LEFT_CLICK);
  }

  // -- entities ------------------------------------------------------------------------

  /** Draw the estate. Called once on load; later changes go through `applyImpact`. */
  setEntities(entities: WorldEntity[]): void {
    const { viewer } = this.globe;
    // Track how many logical entities share each site so they can be fanned apart rather
    // than stacked into one unreadable point.
    const siteCounts = new Map<string, number>();

    for (const entity of entities) {
      if (!entity.location) continue;
      if (!DEFAULT_VISIBLE_TYPES.has(entity.type)) continue;

      const siteKey = `${entity.location.lat.toFixed(3)},${entity.location.lon.toFixed(3)}`;
      const index = siteCounts.get(siteKey) ?? 0;
      siteCounts.set(siteKey, index + 1);

      const isPhysical = PHYSICAL_TYPES.has(entity.type);
      const altitude = isPhysical ? 0 : altitudeFor(entity.type, index);
      const position = Cartesian3.fromDegrees(
        entity.location.lon,
        entity.location.lat,
        altitude,
      );

      const marker = viewer.entities.add({
        id: `entity:${entity.id}`,
        position,
        point: {
          pixelSize: CRITICALITY_SIZE[entity.criticality],
          color: cesiumColor(ENTITY_BASE, 0.95),
          outlineColor: cesiumColor('#0b1020', 0.9),
          outlineWidth: 2,
          heightReference: isPhysical
            ? HeightReference.CLAMP_TO_GROUND
            : HeightReference.NONE,
          // Markers shrink with distance so a zoomed-out globe is not a wall of dots.
          scaleByDistance: new NearFarScalar(1.0e5, 1.35, 3.0e7, 0.45),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        label: {
          text: `${ENTITY_GLYPHS[entity.type]}  ${entity.name}`,
          font: '500 12px "Inter", system-ui, sans-serif',
          fillColor: cesiumColor('#dfe5f2', 0.92),
          outlineColor: cesiumColor('#05070d', 0.95),
          outlineWidth: 3,
          style: LabelStyle.FILL_AND_OUTLINE,
          horizontalOrigin: HorizontalOrigin.LEFT,
          verticalOrigin: VerticalOrigin.CENTER,
          pixelOffset: new Cartesian2(14, 0),
          distanceDisplayCondition: LABEL_VISIBILITY,
          // Labels are the first thing to become noise when zoomed out.
          scaleByDistance: new NearFarScalar(1.0e5, 1.0, 1.2e7, 0.0),
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
          showBackground: false,
        },
      });

      this.records.set(entity.id, { entity, marker, availability: 1 });
    }
    this.globe.requestRender();
  }

  /** Draw the dependency arcs between entities that both have positions. */
  setEdges(edges: DependencyEdge[]): void {
    const { viewer } = this.globe;
    for (const edge of edges) {
      const from = this.records.get(edge.source_entity_id);
      const to = this.records.get(edge.target_entity_id);
      if (!from || !to) continue;

      const positions = [
        from.marker.position!.getValue(viewer.clock.currentTime)!,
        to.marker.position!.getValue(viewer.clock.currentTime)!,
      ];
      const line = viewer.entities.add({
        id: `edge:${edge.id}`,
        polyline: {
          positions,
          width: 1.2,
          // Static material. A CallbackProperty here would rebuild every arc per frame.
          material: new ColorMaterialProperty(cesiumColor(EDGE_IDLE)),
          // GEODESIC, not NONE. A straight chord between Singapore and Virginia passes
          // through the planet and — with depth testing off — renders as a line floating
          // across Africa. A great-circle arc follows the surface and reads as a route.
          arcType: ArcType.GEODESIC,
          // Below this the arcs merge into a haze and cost more than they communicate.
          distanceDisplayCondition: new DistanceDisplayCondition(0, 25_000_000),
        },
        show: this.edgesVisible,
      });
      this.edgeEntities.set(edge.id, line);
    }
    this.globe.requestRender();
  }

  setEdgesVisible(visible: boolean): void {
    this.edgesVisible = visible;
    for (const line of this.edgeEntities.values()) {
      line.show = visible;
    }
    this.globe.requestRender();
  }

  // -- events --------------------------------------------------------------------------

  /** Draw event markers and their exposure radius. */
  setEvents(events: WorldEvent[]): void {
    const { viewer } = this.globe;
    const seen = new Set<string>();

    for (const event of events) {
      if (!event.location) continue;
      seen.add(event.id);
      if (this.eventEntities.has(event.id)) continue;

      const color = severityColor(event.severity);
      const marker = viewer.entities.add({
        id: `event:${event.id}`,
        position: Cartesian3.fromDegrees(event.location.lon, event.location.lat),
        point: {
          pixelSize: 14,
          color: cesiumColor(color, 0.95),
          outlineColor: cesiumColor('#ffffff', 0.55),
          outlineWidth: 2,
          heightReference: HeightReference.CLAMP_TO_GROUND,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
        // Static axes, not a pulsing CallbackProperty: the baseline measured a pulsing
        // ground ellipse at 32.4 ms/frame for 58 discs versus 1.4 ms/frame static. An
        // imperceptible wobble is not worth 20× the frame budget.
        ellipse:
          event.exposure_radius_km > 0
            ? {
                semiMajorAxis: event.exposure_radius_km * 1000,
                semiMinorAxis: event.exposure_radius_km * 1000,
                material: new ColorMaterialProperty(cesiumColor(color, 0.1)),
                outline: true,
                outlineColor: cesiumColor(color, 0.4),
                outlineWidth: 1,
                heightReference: HeightReference.CLAMP_TO_GROUND,
              }
            : undefined,
        label: {
          text: eventGlyph(event),
          font: '600 12px "Inter", system-ui, sans-serif',
          fillColor: cesiumColor(color, 1),
          outlineColor: cesiumColor('#05070d', 0.95),
          outlineWidth: 3,
          style: LabelStyle.FILL_AND_OUTLINE,
          horizontalOrigin: HorizontalOrigin.LEFT,
          pixelOffset: new Cartesian2(16, -12),
          distanceDisplayCondition: LABEL_VISIBILITY,
          disableDepthTestDistance: Number.POSITIVE_INFINITY,
        },
      });
      this.eventEntities.set(event.id, marker);
    }

    for (const [id, marker] of this.eventEntities) {
      if (!seen.has(id)) {
        viewer.entities.remove(marker);
        this.eventEntities.delete(id);
      }
    }
    this.globe.requestRender();
  }

  // -- impact --------------------------------------------------------------------------

  /**
   * Recolour the estate from a blast-radius result.
   *
   * Only entities whose availability actually changed are written, so a repeat call after
   * an unchanged analysis costs nothing.
   */
  applyImpact(impacted: ImpactedEntity[]): void {
    const byId = new Map(impacted.map((row) => [row.entity_id, row]));
    for (const [id, record] of this.records) {
      const availability = byId.get(id)?.availability ?? 1;
      if (Math.abs(availability - record.availability) < 1e-6) continue;
      record.availability = availability;
      const color = availabilityColor(availability);
      if (record.marker.point) {
        record.marker.point.color = new ConstantProperty(cesiumColor(color, 0.95));
        record.marker.point.pixelSize = new ConstantProperty(
          CRITICALITY_SIZE[record.entity.criticality] + (availability < 0.999 ? 4 : 0),
        );
      }
      if (record.marker.label) {
        record.marker.label.fillColor = new ConstantProperty(
          cesiumColor(availability < 0.999 ? color : '#dfe5f2', 0.95),
        );
      }
    }
    this.globe.requestRender();
  }

  /** Reset every entity to its unimpacted appearance. */
  clearImpact(): void {
    this.applyImpact([]);
    this.clearPaths();
  }

  // -- selection & highlight ------------------------------------------------------------

  setSelected(entityId: string | null): void {
    if (this.selectedEntityId === entityId) return;
    const previous = this.selectedEntityId;
    this.selectedEntityId = entityId;
    if (previous) this.restoreOutline(previous);
    if (entityId) {
      const record = this.records.get(entityId);
      if (record?.marker.point) {
        record.marker.point.outlineColor = new ConstantProperty(cesiumColor(SELECTION, 1));
        record.marker.point.outlineWidth = new ConstantProperty(3);
      }
    }
    this.globe.requestRender();
  }

  /** Emphasise a set of entities and dim the rest, so a cohort reads at a glance. */
  setHighlighted(entityIds: string[]): void {
    this.highlighted = new Set(entityIds);
    const dim = this.highlighted.size > 0;
    for (const [id, record] of this.records) {
      const lit = !dim || this.highlighted.has(id);
      if (record.marker.label) {
        record.marker.label.show = new ConstantProperty(lit);
      }
      if (record.marker.point) {
        record.marker.point.color = new ConstantProperty(
          cesiumColor(availabilityColor(record.availability), lit ? 0.95 : 0.25),
        );
      }
    }
    this.globe.requestRender();
  }

  private restoreOutline(entityId: string): void {
    const record = this.records.get(entityId);
    if (record?.marker.point) {
      record.marker.point.outlineColor = new ConstantProperty(cesiumColor('#0b1020', 0.9));
      record.marker.point.outlineWidth = new ConstantProperty(2);
    }
  }

  // -- paths ---------------------------------------------------------------------------

  /** Draw a dependency path as a lit chain, without animating it. */
  showPath(entityIds: string[], color = EDGE_ACTIVE): void {
    this.clearPaths();
    const { viewer } = this.globe;
    for (let index = 0; index < entityIds.length - 1; index += 1) {
      const from = this.records.get(entityIds[index]!);
      const to = this.records.get(entityIds[index + 1]!);
      if (!from || !to) continue;
      this.pathEntities.push(
        viewer.entities.add({
          polyline: {
            positions: [
              from.marker.position!.getValue(viewer.clock.currentTime)!,
              to.marker.position!.getValue(viewer.clock.currentTime)!,
            ],
            width: 3,
            material: new PolylineGlowMaterialProperty({
              color: cesiumColor(color, 0.9),
              glowPower: 0.25,
            }),
            arcType: ArcType.GEODESIC,
          },
        }),
      );
    }
    this.globe.requestRender();
  }

  clearPaths(): void {
    const { viewer } = this.globe;
    for (const entity of this.pathEntities) viewer.entities.remove(entity);
    this.pathEntities.length = 0;
    this.animationToken += 1; // cancels any animation mid-flight
    this.globe.requestRender();
  }

  /**
   * Animate impact propagating along one or more paths.
   *
   * This is the product's signature moment: the operator clicks an event and watches the
   * consequence travel from a supplier in Taiwan to customers in Singapore.
   *
   * Holds continuous rendering only while it runs. The dash offset is the one
   * `CallbackProperty` in this file, and it is on a screen-space polyline — not on ground
   * geometry, which is what made the baseline's earthquake discs expensive.
   */
  async animatePropagation(paths: string[][]): Promise<void> {
    this.clearPaths();
    if (paths.length === 0) return;

    const token = (this.animationToken += 1);
    const { viewer } = this.globe;
    const release = this.globe.holdContinuousRender();
    const start = performance.now();

    try {
      const longest = Math.max(...paths.map((path) => path.length));
      for (let hop = 0; hop < longest - 1; hop += 1) {
        if (token !== this.animationToken) return; // superseded or cleared

        for (const path of paths) {
          const from = this.records.get(path[hop]!);
          const to = this.records.get(path[hop + 1]!);
          if (!from || !to) continue;

          // Later hops read as more severe: by the time impact reaches a customer-facing
          // service, it should look like it.
          const color = hop >= 2 ? EDGE_CRITICAL : EDGE_ACTIVE;
          this.pathEntities.push(
            viewer.entities.add({
              polyline: {
                positions: [
                  from.marker.position!.getValue(viewer.clock.currentTime)!,
                  to.marker.position!.getValue(viewer.clock.currentTime)!,
                ],
                width: 3.5,
                material: new PolylineDashMaterialProperty({
                  color: cesiumColor(color, 0.95),
                  gapColor: Color.TRANSPARENT,
                  dashLength: 22,
                  dashPattern: new CallbackProperty(
                    () => dashPattern(performance.now() - start),
                    false,
                  ),
                }),
                arcType: ArcType.GEODESIC,
              },
            }),
          );
        }
        this.globe.requestRender();
        await delay(HOP_DURATION_MS);
      }

      // Settle into a static glow so the finished path costs nothing to keep on screen.
      if (token === this.animationToken) {
        for (const entity of this.pathEntities) {
          if (entity.polyline) {
            entity.polyline.material = new PolylineGlowMaterialProperty({
              color: cesiumColor(EDGE_CRITICAL, 0.85),
              glowPower: 0.22,
            });
          }
        }
      }
    } finally {
      release();
      this.globe.requestRender();
    }
  }

  // -- lookup --------------------------------------------------------------------------

  positionOf(entityId: string): { lat: number; lon: number } | null {
    const record = this.records.get(entityId);
    if (!record?.entity.location) return null;
    return { lat: record.entity.location.lat, lon: record.entity.location.lon };
  }

  destroy(): void {
    this.handler?.destroy();
    this.handler = null;
  }
}

/** Marching dashes, as a 16-bit pattern rotated over time. */
function dashPattern(elapsedMs: number): number {
  const shift = Math.floor(elapsedMs / 55) % 16;
  const base = 0b1111000011110000;
  return ((base << shift) | (base >>> (16 - shift))) & 0xffff;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function eventGlyph(event: WorldEvent): string {
  const magnitude = event.metadata['magnitude'];
  if (typeof magnitude === 'number') return `M${magnitude.toFixed(1)}`;
  if (event.category === 'SECURITY_VULNERABILITY') return '⚠ CVE';
  if (event.category === 'CLOUD_INCIDENT') return '☁ INCIDENT';
  return event.severity;
}
