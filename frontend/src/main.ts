/**
 * WorldGraph application entry point.
 *
 * Composition root: creates the globe, wires the panels to the store, and owns the
 * workflows that span both — analysing an event, running a simulation, asking the analyst,
 * restoring a share link.
 *
 * Rendering is a single pass driven by store subscription. With this much state a
 * whole-panel rebuild per change is cheaper than diffing, and it removes an entire class
 * of "the panel and the globe disagree" bug.
 */

import './styles/app.css';
import './styles/panels.css';

import { ApiError, api, isSuperseded, setActiveWorkspace } from './api/client.ts';
import { EntityLayer } from './globe/entityLayer.ts';
import { HEALTH_COLORS, SEVERITY_COLORS } from './globe/palette.ts';
import { createGlobe, type GlobeHandles } from './globe/viewer.ts';
import { readShareState, shareUrl, writeShareState } from './state/sharelink.ts';
import { store } from './state/store.ts';
import { bind, bindAll, el, replaceChildren, utcClock } from './ui/dom.ts';
import {
  renderAnalysis,
  renderCoverage,
  renderDependencies,
  renderEntityDetail,
  renderEventDetail,
  renderEvents,
  renderFeeds,
  renderHeadlineStats,
  renderPlan,
  renderRisks,
  renderSimulation,
  renderTimeline,
  type PanelActions,
} from './ui/panels.ts';
import type { AnalystDirective, MaterialRisk, ReplayScenario } from './types.ts';

/** Suggested commands under the command bar. Every one is routed deterministically. */
const SUGGESTIONS = [
  'What can hurt us right now?',
  'Investigate the Taiwan earthquake.',
  'Show me our critical infrastructure.',
  'What happens if Singapore goes offline?',
  'Show me our vulnerability exposure.',
  'What should we do?',
];

/** Session-scoped first-run suppression, following the baseline's policy: a mission
 *  choice is enthusiasm, not "never show me this again". */
const FIRST_RUN_SESSION_KEY = 'worldgraph:first-run:session';

type DockTab = 'timeline' | 'simulation' | 'dependencies';

class WorldGraphApp {
  private globe!: GlobeHandles;
  private layer!: EntityLayer;
  private dockTab: DockTab = 'timeline';
  private clockTimer = 0;
  private pollTimer = 0;
  /**
   * The share state the *user* arrived with.
   *
   * Captured before anything else runs, because the app writes to the same URL: the globe
   * persists its camera on idle, and if that lands before the share state is read back,
   * WorldGraph mistakes its own bookkeeping for a link somebody sent — and skips the
   * first-run launcher on a plain visit.
   */
  private arrivalShareState = readShareState();

  async start(): Promise<void> {
    this.installStaticHandlers();
    store.subscribe(() => this.render());
    this.startClock();

    try {
      const config = await api.config();
      store.update({ config });
      await this.initGlobe(config.cesium_ion_token);
    } catch (error) {
      this.reportError('startup', error);
      // The globe is presentation. Without it the panels still work, so keep going.
      await this.initGlobe(null).catch(() => undefined);
    }

    await this.loadWorkspaces();
    await this.loadWorld();
    this.startPolling();
    await this.restoreShareLinkOrOfferMissions();
  }

  // ----------------------------------------------------------------------------------
  // Bootstrap
  // ----------------------------------------------------------------------------------

  private async initGlobe(ionToken: string | null): Promise<void> {
    const container = document.getElementById('globe');
    if (!container) throw new Error('Missing #globe container');

    this.globe = await createGlobe({
      container,
      cesiumIonToken: ionToken,
      onCameraIdle: () => this.persistShareState(),
    });
    this.layer = new EntityLayer({
      globe: this.globe,
      onSelectEntity: (id) => void (id ? this.selectEntity(id) : this.clearSelection()),
      onSelectEvent: (id) => void this.selectEvent(id),
    });
  }

  /**
   * List the workspaces and settle on one.
   *
   * A failure here is not fatal: the backend has a default, so the app carries on
   * unscoped rather than refusing to start over a switcher.
   */
  private async loadWorkspaces(): Promise<void> {
    try {
      const { workspaces, default_id } = await api.workspaces();
      const active = store.get().activeWorkspaceId ?? default_id;
      setActiveWorkspace(active);
      store.update({ workspaces, activeWorkspaceId: active });
      await this.loadWorkspaceDetail();
    } catch (error) {
      if (isSuperseded(error)) return;
      this.reportError('workspaces', error);
    }
  }

  private async loadWorkspaceDetail(): Promise<void> {
    try {
      store.update({ workspaceDetail: await api.currentWorkspace() });
    } catch (error) {
      if (isSuperseded(error)) return;
      store.update({ workspaceDetail: null });
    }
  }

  /**
   * Switch estates.
   *
   * Everything on screen belongs to the estate it came from, so the switch clears all of
   * it before loading the new one — no selection, no analysis, no simulation and no
   * transcript survives. Carrying any of it across would render one estate's finding
   * against another's entities.
   *
   * A workspace that has not been imported yet is imported first, because the alternative
   * — an empty graph with no explanation — looks exactly like an estate with nothing in it.
   */
  private async switchWorkspace(workspaceId: string): Promise<void> {
    if (workspaceId === store.get().activeWorkspaceId) return;

    const done = store.startWork('workspace');
    const target = store.get().workspaces.find((w) => w.id === workspaceId);
    try {
      store.dismissNotice('workspace');
      store.clearEstateState();
      setActiveWorkspace(workspaceId);
      store.update({ activeWorkspaceId: workspaceId, workspaceDetail: null });

      if (target && target.status !== 'READY') {
        store.notify({
          id: 'workspace',
          tone: 'info',
          title: `Importing ${target.name}`,
          detail: 'Reading inventory. WorldGraph reads only and changes nothing.',
        });
        await api.importWorkspace(workspaceId);
        store.dismissNotice('workspace');
      }

      const { workspaces } = await api.workspaces();
      store.update({ workspaces });
      await this.loadWorkspaceDetail();
      await this.loadWorld();
      // A different estate lives in different places. Reframe rather than leaving the
      // camera over a region the new workspace has nothing in.
      this.frameEstate();
    } catch (error) {
      if (isSuperseded(error)) return;
      // Fall back to the workspace that is guaranteed to work, and say why — an empty
      // screen with no explanation is the one outcome that teaches the operator nothing.
      const detail =
        error instanceof ApiError ? error.message : 'The workspace could not be loaded.';
      store.notify({
        id: 'workspace',
        tone: 'error',
        title: `Could not open ${target?.name ?? workspaceId}`,
        detail,
      });
      const fallback = store.get().workspaces.find((w) => w.status === 'READY');
      if (fallback && fallback.id !== workspaceId) {
        setActiveWorkspace(fallback.id);
        store.update({ activeWorkspaceId: fallback.id });
        await this.loadWorkspaceDetail();
        await this.loadWorld();
      }
    } finally {
      done();
    }
  }

  /** Point the camera at wherever this estate actually is. */
  private frameEstate(): void {
    const positions = store
      .get()
      .entities.map((entity) => entity.location)
      .filter((point): point is NonNullable<typeof point> => point !== null)
      .map((point) => ({ lat: point.lat, lon: point.lon }));
    // An estate with no placed entity — every region unrecognised, say — leaves the
    // camera where it is rather than flying to a default that means nothing.
    if (positions.length > 0) void this.globe.flyToEntities(positions);
  }

  private async loadWorld(): Promise<void> {
    const done = store.startWork('world');
    try {
      const [world, events, feeds, risks, timeline, scenarios] = await Promise.all([
        api.world(),
        api.events(40),
        api.feeds(),
        api.risks(),
        api.timeline(60),
        api.replayScenarios(),
      ]);
      const dashboard = await api.dashboard();

      store.update({
        entities: world.entities,
        edges: world.edges,
        metrics: world.metrics,
        events,
        feeds,
        risks,
        timeline,
        replayScenarios: scenarios,
        dashboard,
      });

      this.layer.setEntities(world.entities);
      this.layer.setEdges(world.edges);
      this.layer.setEvents(events);
      this.hideGlobeLoading();
    } catch (error) {
      this.reportError('world', error);
      this.hideGlobeLoading();
    } finally {
      done();
    }
  }

  /**
   * Poll the cheap, changing things.
   *
   * Only feeds, events and the timeline — the estate does not change on its own, so
   * re-fetching the whole world on a timer would be waste.
   */
  private startPolling(): void {
    this.pollTimer = window.setInterval(async () => {
      if (document.hidden) return; // a backgrounded tab does not need fresh feeds
      try {
        const [feeds, events, timeline] = await Promise.all([
          api.feeds(),
          api.events(40),
          api.timeline(60),
        ]);
        store.update({ feeds, events, timeline });
        this.layer.setEvents(events);
        store.dismissNotice('poll');
      } catch (error) {
        if (isSuperseded(error)) return;
        // A failed poll is worth saying once, precisely, and not repeating.
        store.notify({
          id: 'poll',
          tone: 'warning',
          title: 'Live updates paused',
          detail:
            error instanceof ApiError
              ? `${error.message} Showing the last successful snapshot.`
              : 'The backend stopped responding. Showing the last successful snapshot.',
        });
      }
    }, 30_000);
  }

  private startClock(): void {
    const clock = bind('clock');
    const tick = () => {
      clock.textContent = utcClock();
    };
    tick();
    this.clockTimer = window.setInterval(tick, 15_000);
  }

  // ----------------------------------------------------------------------------------
  // Workflows
  // ----------------------------------------------------------------------------------

  private async selectEntity(entityId: string): Promise<void> {
    store.update({ selectedEntityId: entityId, selectedEventId: null, selectedEvent: null });
    this.layer.setSelected(entityId);
    const done = store.startWork('entity');
    try {
      const detail = await api.entity(entityId);
      store.update({ selectedEntity: detail });
      const position = this.layer.positionOf(entityId);
      if (position) await this.globe.flyTo(position.lat, position.lon, 2_400_000);
      this.persistShareState();
    } catch (error) {
      if (!isSuperseded(error)) this.reportError('entity', error);
    } finally {
      done();
    }
  }

  private clearSelection(): void {
    store.update({ selectedEntityId: null, selectedEntity: null });
    this.layer.setSelected(null);
    this.persistShareState();
  }

  private async selectEvent(eventId: string): Promise<void> {
    store.update({ selectedEventId: eventId, selectedEntityId: null, selectedEntity: null });
    this.layer.setSelected(null);
    const done = store.startWork('event');
    try {
      const detail = await api.event(eventId);
      store.update({ selectedEvent: detail });
      if (detail.event.location) {
        await this.globe.flyTo(detail.event.location.lat, detail.event.location.lon, 3_000_000);
      }
      this.persistShareState();
    } catch (error) {
      if (!isSuperseded(error)) this.reportError('event', error);
    } finally {
      done();
    }
  }

  /** The hero moment: analyse, recolour the globe, animate the propagation. */
  private async analyzeEvent(eventId: string): Promise<void> {
    const done = store.startWork('analysis');
    try {
      const analysis = await api.blastRadius({ event_id: eventId });
      store.update({ analysis, plan: null });

      const impacted = [...analysis.direct_impact, ...analysis.indirect_impact];
      this.layer.applyImpact(impacted);
      this.layer.setHighlighted(impacted.map((row) => row.entity_id));

      // Frame the *origin and direct impact*, not the whole radius. Indirect impact
      // reaches customers on three continents, and framing all of it zooms out until
      // nothing is legible. The propagation animation carries the eye outward from here.
      const framed = [
        ...analysis.origin_ids,
        ...analysis.direct_impact.map((row) => row.entity_id),
      ];
      const positions = framed
        .map((id) => this.layer.positionOf(id))
        .filter((position): position is { lat: number; lon: number } => position !== null);
      if (positions.length > 0) await this.globe.flyToEntities(positions);

      const paths = analysis.critical_paths.map((path) => path.hops.map((hop) => hop.entity_id));
      if (paths.length > 0) {
        store.update({ focusedPath: paths[0]! });
        await this.layer.animatePropagation(paths.slice(0, 3));
      }
      this.setDockTab('timeline');
      await this.refreshTimeline();
      this.persistShareState();
    } catch (error) {
      if (!isSuperseded(error)) this.reportError('analysis', error);
    } finally {
      done();
    }
  }

  private async traceDependencies(entityId: string): Promise<void> {
    const done = store.startWork('trace');
    try {
      const result = await api.trace(entityId, 'dependents', 4);
      const ids = result.steps.map((step) => step.entity_id);
      this.layer.setHighlighted(ids);
      const deepest = result.steps.reduce(
        (best, step) => (step.depth > best.depth ? step : best),
        result.steps[0]!,
      );
      this.layer.showPath(deepest.path);
      store.update({ highlightedEntityIds: ids, focusedPath: deepest.path });
      this.setDockTab('dependencies');
      this.persistShareState();
    } catch (error) {
      if (!isSuperseded(error)) this.reportError('trace', error);
    } finally {
      done();
    }
  }

  private async ensureScenario(name: string): Promise<string> {
    const existing = store.get().scenario;
    if (existing) return existing.id;
    const scenario = await api.createScenario({ name });
    store.update({ scenario, simulationActive: true });
    return scenario.id;
  }

  private async addFailure(entityId: string, health: string): Promise<void> {
    const done = store.startWork('simulation');
    try {
      const entityName = store.entityName(entityId);
      const scenarioId = await this.ensureScenario(`What-if: ${entityName} ${health}`);
      const scenario = await api.addOverride(scenarioId, { target_id: entityId, health });
      store.update({ scenario, simulationActive: true });
      await this.refreshComparison();
      this.setDockTab('simulation');
    } catch (error) {
      if (!isSuperseded(error)) this.reportError('simulation', error);
    } finally {
      done();
    }
  }

  private async removeOverride(overrideId: string): Promise<void> {
    const scenario = store.get().scenario;
    if (!scenario) return;
    try {
      const updated = await api.removeOverride(scenario.id, overrideId);
      store.update({ scenario: updated });
      await this.refreshComparison();
    } catch (error) {
      this.reportError('simulation', error);
    }
  }

  private async refreshComparison(): Promise<void> {
    const scenario = store.get().scenario;
    if (!scenario) return;
    if (scenario.overrides.length === 0) {
      store.update({ comparison: null });
      this.layer.clearImpact();
      this.layer.setHighlighted([]);
      return;
    }
    const comparison = await api.compareScenario(scenario.id);
    store.update({ comparison });

    const impacted = comparison.newly_impacted;
    this.layer.applyImpact(impacted);
    this.layer.setHighlighted(impacted.map((row) => row.entity_id));
    if (comparison.cascade_paths.length > 0) {
      const paths = comparison.cascade_paths.map((path) => path.hops.map((hop) => hop.entity_id));
      await this.layer.animatePropagation(paths.slice(0, 3));
    }
    await this.refreshTimeline();
    this.persistShareState();
  }

  private async exitSimulation(): Promise<void> {
    const scenario = store.get().scenario;
    if (scenario) {
      // Reset rather than delete: the operator may want to re-enter the same scenario,
      // and a share link that names it must not 404.
      await api.resetScenario(scenario.id).catch(() => undefined);
    }
    store.update({ scenario: null, comparison: null, simulationActive: false });
    this.layer.clearImpact();
    this.layer.setHighlighted([]);
    // Re-apply the standing analysis, if any: leaving simulation returns the operator to
    // what they were looking at, not to a blank globe.
    const analysis = store.get().analysis;
    if (analysis) {
      this.layer.applyImpact([...analysis.direct_impact, ...analysis.indirect_impact]);
    }
    this.persistShareState();
  }

  private async generatePlan(): Promise<void> {
    const done = store.startWork('plan');
    const { analysis, scenario } = store.get();
    try {
      const plan = await api.responsePlan(
        scenario && scenario.overrides.length > 0
          ? { scenario_id: scenario.id }
          : analysis
            ? { analysis_id: analysis.id }
            : {},
      );
      store.update({ plan });
      // Deliberately does NOT move the dock. The plan renders in the right rail; yanking
      // the operator away from the simulation table they are reading is disruptive and
      // unrelated to what they asked for.
      await this.refreshTimeline();
    } catch (error) {
      if (!isSuperseded(error)) this.reportError('plan', error);
    } finally {
      done();
    }
  }

  private async ask(message: string): Promise<void> {
    const trimmed = message.trim();
    if (!trimmed) return;
    const { selectedEntityId, selectedEventId, scenario } = store.get();

    store.appendMessage({ role: 'operator', text: trimmed, at: Date.now() });
    const done = store.startWork('ask');
    try {
      const response = await api.ask({
        message: trimmed,
        selected_entity_id: selectedEntityId,
        selected_event_id: selectedEventId,
        active_scenario_id: scenario?.id ?? null,
      });

      store.appendMessage({
        role: 'analyst',
        text: response.answer,
        at: Date.now(),
        engine: response.engine,
        tools: response.tool_calls.map((call) => call.tool),
        degradedReason: response.degraded_reason,
      });

      if (response.degraded_reason) {
        store.notify({
          id: 'analyst-degraded',
          tone: 'warning',
          title: 'AI analyst unavailable',
          detail: `${response.degraded_reason} Answered with WorldGraph's deterministic analyst instead; analysis and simulation are unaffected.`,
        });
      }

      if (response.active_scenario_id && response.active_scenario_id !== scenario?.id) {
        const created = await api.scenario(response.active_scenario_id);
        store.update({ scenario: created, simulationActive: true });
        await this.refreshComparison();
      } else if (scenario) {
        const refreshed = await api.scenario(scenario.id);
        store.update({ scenario: refreshed });
        await this.refreshComparison();
      }

      await this.applyDirectives(response.directives);

      // A response plan read as monospace transcript loses the per-action urgency badges
      // and the rationale layout. When the analyst generated one, render it structurally
      // in the detail panel too — regeneration is deterministic, so the two agree.
      if (response.tool_calls.some((call) => call.tool === 'generate_response_plan')) {
        await this.generatePlan();
      }
      await this.refreshTimeline();
    } catch (error) {
      if (isSuperseded(error)) return;
      const detail =
        error instanceof ApiError
          ? error.message
          : 'The analyst could not be reached.';
      store.appendMessage({ role: 'system', text: detail, at: Date.now() });
      store.notify({
        id: 'ask',
        tone: 'error',
        title: 'Analyst request failed',
        detail: `${detail} Infrastructure analysis and simulation remain available.`,
      });
    } finally {
      done();
    }
  }

  /** Carry out the camera and highlight instructions the analyst returned. */
  private async applyDirectives(directives: AnalystDirective[]): Promise<void> {
    for (const directive of directives) {
      switch (directive.kind) {
        case 'focus_entity':
          if (directive.entity_id) {
            const position = this.layer.positionOf(directive.entity_id);
            if (position) await this.globe.flyTo(position.lat, position.lon, 2_400_000);
          }
          break;
        case 'focus_event': {
          const event = store.event(directive.event_id);
          if (event?.location) {
            await this.globe.flyTo(event.location.lat, event.location.lon, 3_000_000);
          }
          break;
        }
        case 'annotate':
          if (directive.entity_ids?.length) {
            this.layer.setHighlighted(directive.entity_ids);
            store.update({ highlightedEntityIds: directive.entity_ids });
            const positions = directive.entity_ids
              .map((id) => this.layer.positionOf(id))
              .filter((p): p is { lat: number; lon: number } => p !== null);
            if (positions.length > 0) await this.globe.flyToEntities(positions);
          }
          break;
        case 'show_path':
          if (directive.path?.length) {
            this.layer.showPath(directive.path);
            store.update({ focusedPath: directive.path });
          }
          break;
        case 'show_layer':
          if (directive.layer === 'dependencies' && typeof directive.visible === 'boolean') {
            this.layer.setEdgesVisible(directive.visible);
            store.update({ showDependencies: directive.visible });
          }
          break;
        default:
          // An unknown directive is not an error — the backend may know a verb this build
          // does not. Ignoring it is correct; guessing would not be.
          break;
      }
    }
  }

  private async refreshTimeline(): Promise<void> {
    try {
      store.update({ timeline: await api.timeline(60) });
    } catch {
      // The timeline is a record, not a control. A failed refresh is not worth a banner.
    }
  }

  private async runMission(scenario: ReplayScenario): Promise<void> {
    this.dismissFirstRun();
    await this.selectEvent(scenario.event_id);
    await this.analyzeEvent(scenario.event_id);
  }

  private async focusRisk(risk: MaterialRisk): Promise<void> {
    this.layer.setHighlighted(risk.focus_entity_ids);
    store.update({ highlightedEntityIds: risk.focus_entity_ids });
    const positions = risk.focus_entity_ids
      .map((id) => this.layer.positionOf(id))
      .filter((p): p is { lat: number; lon: number } => p !== null);
    if (positions.length > 0) await this.globe.flyToEntities(positions);
    const first = risk.focus_entity_ids[0];
    if (first) await this.selectEntity(first);
  }

  // ----------------------------------------------------------------------------------
  // Share links & first run
  // ----------------------------------------------------------------------------------

  private persistShareState(): void {
    const state = store.get();
    writeShareState({
      eventId: state.selectedEventId,
      entityId: state.selectedEntityId,
      scenarioId: state.scenario?.id ?? null,
      camera: this.globe ? this.globe.cameraState() : null,
      path: state.focusedPath,
      showDependencies: state.showDependencies,
      viewMode: state.viewMode,
    });
  }

  private async restoreShareLinkOrOfferMissions(): Promise<void> {
    const shared = this.arrivalShareState;

    if (shared === null) {
      // Malformed link. Say so rather than silently showing a default view the sender
      // did not send — and rather than half-applying it.
      store.notify({
        id: 'share',
        tone: 'warning',
        title: 'Share link not understood',
        detail: 'The link contained an unrecognised value, so it was ignored entirely.',
      });
      this.offerMissions();
      return;
    }

    const hasSharedState = Boolean(
      shared.eventId || shared.entityId || shared.scenarioId || shared.camera,
    );
    if (!hasSharedState) {
      this.offerMissions();
      return;
    }

    // A share link author already chose the experience; never interrupt them with the
    // first-run dialog.
    this.dismissFirstRun();
    store.update({ viewMode: shared.viewMode, showDependencies: shared.showDependencies });
    this.layer.setEdgesVisible(shared.showDependencies);

    if (shared.camera) this.globe.setCameraState(shared.camera);
    if (shared.scenarioId) {
      try {
        const scenario = await api.scenario(shared.scenarioId);
        store.update({ scenario, simulationActive: true });
        await this.refreshComparison();
      } catch {
        store.notify({
          id: 'share-scenario',
          tone: 'info',
          title: 'Shared scenario unavailable',
          detail: 'The simulation in this link no longer exists on the server.',
        });
      }
    }
    if (shared.eventId) await this.selectEvent(shared.eventId);
    if (shared.entityId) await this.selectEntity(shared.entityId);
    if (shared.path.length > 0) this.layer.showPath(shared.path);
  }

  private offerMissions(): void {
    if (sessionStorage.getItem(FIRST_RUN_SESSION_KEY) === '1') return;
    const dialog = bind<HTMLDialogElement>('first-run');
    const missions = bind('missions');
    const { replayScenarios, dashboard } = store.get();

    replaceChildren(
      missions,
      ...replayScenarios.map((scenario) => {
        const button = el(
          'button',
          { type: 'button', class: 'mission' },
          el('div', { class: 'mission__title', text: scenario.name }),
          el('div', { class: 'mission__summary', text: scenario.summary }),
        );
        button.addEventListener('click', () => void this.runMission(scenario));
        return button;
      }),
      (() => {
        const explore = el(
          'button',
          { type: 'button', class: 'mission' },
          el('div', {
            class: 'mission__title',
            text: `Explore ${store.get().dashboard?.organization ?? 'the estate'}`,
          }),
          el('div', {
            class: 'mission__summary',
            text: 'Open the estate with no incident selected and look around.',
          }),
        );
        explore.addEventListener('click', () => this.dismissFirstRun());
        return explore;
      })(),
    );

    bind('first-run-disclaimer').textContent =
      dashboard?.data_disclaimer ??
      'Each workspace states its own provenance. World events are labelled individually.';

    if (!dialog.open) dialog.showModal();
  }

  private dismissFirstRun(): void {
    const dialog = bind<HTMLDialogElement>('first-run');
    if (dialog.open) dialog.close();
    // Session, not durable: a new operator needs the launcher explained more than once.
    sessionStorage.setItem(FIRST_RUN_SESSION_KEY, '1');
    store.update({ firstRunDismissed: true });
  }

  /**
   * The estate selector and the badge that says what kind of data is on screen.
   *
   * Unavailable workspaces stay in the list, disabled, carrying their failure reason —
   * removing them would make a configuration problem look like an estate that simply is
   * not there.
   */
  private renderWorkspaceSwitcher(state: ReturnType<typeof store.get>): void {
    const select = bind<HTMLSelectElement>('workspace-select');
    const active = state.activeWorkspaceId;

    replaceChildren(
      select,
      ...state.workspaces.map((workspace) => {
        const suffix =
          workspace.status === 'UNAVAILABLE'
            ? ' (unavailable)'
            : workspace.status === 'NOT_LOADED'
              ? ' (not imported)'
              : '';
        const option = el('option', {
          value: workspace.id,
          text: `${workspace.name}${suffix}`,
          title: workspace.message ?? workspace.description,
        }) as HTMLOptionElement;
        option.disabled = workspace.status === 'UNAVAILABLE';
        option.selected = workspace.id === active;
        return option;
      }),
    );
    select.disabled = state.workspaces.length < 2 || store.isBusy('workspace');

    const workspace = store.activeWorkspace();
    const badge = bind('workspace-badge');
    if (!workspace) {
      badge.textContent = '';
      badge.hidden = true;
      return;
    }
    badge.hidden = false;
    if (workspace.kind === 'DEMO') {
      badge.textContent = 'SYNTHETIC ESTATE';
      badge.dataset['tone'] = 'synthetic';
    } else if (workspace.mode === 'REPLAY') {
      // A recording, and it must never read as a live view.
      badge.textContent = 'SNAPSHOT · READ ONLY';
      badge.dataset['tone'] = 'replay';
    } else {
      badge.textContent = 'IMPORTED · READ ONLY';
      badge.dataset['tone'] = 'live';
    }
    badge.title = state.workspaceDetail?.disclaimer ?? workspace.description;
  }

  // ----------------------------------------------------------------------------------
  // Static handlers
  // ----------------------------------------------------------------------------------

  private installStaticHandlers(): void {
    bind<HTMLSelectElement>('workspace-select').addEventListener('change', (event) => {
      const target = event.target as HTMLSelectElement;
      void this.switchWorkspace(target.value);
    });

    bind<HTMLFormElement>('command-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const input = bind<HTMLInputElement>('command-input');
      const value = input.value;
      input.value = '';
      void this.ask(value);
    });

    for (const button of bindAll<HTMLButtonElement>('.mode-switch__button')) {
      button.addEventListener('click', () => {
        const mode = button.dataset['view'] === 'engineer' ? 'engineer' : 'executive';
        store.update({ viewMode: mode });
        this.persistShareState();
      });
    }

    for (const tab of bindAll<HTMLButtonElement>('.dock__tab')) {
      tab.addEventListener('click', () => this.setDockTab(tab.dataset['tab'] as DockTab));
    }

    document.querySelector('[data-action="share"]')?.addEventListener('click', () => {
      const state = store.get();
      const url = shareUrl({
        eventId: state.selectedEventId,
        entityId: state.selectedEntityId,
        scenarioId: state.scenario?.id ?? null,
        camera: this.globe ? this.globe.cameraState() : null,
        path: state.focusedPath,
        showDependencies: state.showDependencies,
        viewMode: state.viewMode,
      });
      void navigator.clipboard
        ?.writeText(url)
        .then(() =>
          store.notify({
            id: 'share-copied',
            tone: 'info',
            title: 'Link copied',
            detail: 'It carries the incident, entity, camera and simulation — and no credentials.',
          }),
        )
        .catch(() =>
          store.notify({
            id: 'share-copied',
            tone: 'warning',
            title: 'Could not copy',
            detail: `Copy it from the address bar: ${url}`,
          }),
        );
    });

    document
      .querySelector('[data-action="toggle-dependencies"]')
      ?.addEventListener('click', () => {
        const next = !store.get().showDependencies;
        store.update({ showDependencies: next });
        this.layer.setEdgesVisible(next);
        this.persistShareState();
      });

    document.querySelector('[data-action="reset-view"]')?.addEventListener('click', () => {
      this.layer.setHighlighted([]);
      this.layer.clearPaths();
      store.update({ highlightedEntityIds: [], focusedPath: [] });
      void this.globe.flyTo(12, 60, 26_000_000);
      this.persistShareState();
    });

    document
      .querySelector('[data-action="exit-simulation"]')
      ?.addEventListener('click', () => void this.exitSimulation());

    document
      .querySelector('[data-action="dismiss-first-run"]')
      ?.addEventListener('click', () => this.dismissFirstRun());

    // Escape clears the selection; the command bar keeps focus for typing.
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && document.activeElement?.tagName !== 'INPUT') {
        this.clearSelection();
      }
      if (event.key === '/' && document.activeElement?.tagName !== 'INPUT') {
        event.preventDefault();
        bind<HTMLInputElement>('command-input').focus();
      }
    });

    window.addEventListener('beforeunload', () => {
      window.clearInterval(this.clockTimer);
      window.clearInterval(this.pollTimer);
    });
  }

  private setDockTab(tab: DockTab): void {
    this.dockTab = tab;
    for (const button of bindAll<HTMLButtonElement>('.dock__tab')) {
      button.classList.toggle('is-active', button.dataset['tab'] === tab);
    }
    this.render();
  }

  private reportError(scope: string, error: unknown): void {
    const message =
      error instanceof ApiError
        ? error.message
        : error instanceof Error
          ? error.message
          : 'Unexpected failure.';
    // Never a generic "something went wrong": name the operation and say what still works.
    store.notify({
      id: scope,
      tone: 'error',
      title: `${scope} unavailable`,
      detail: `${message} Other WorldGraph functions remain available.`,
    });
  }

  private hideGlobeLoading(): void {
    const loading = document.querySelector('[data-bind="globe-loading"]');
    loading?.setAttribute('hidden', '');
  }

  // ----------------------------------------------------------------------------------
  // Render
  // ----------------------------------------------------------------------------------

  private render(): void {
    const state = store.get();
    const actions: PanelActions = {
      selectEvent: (id) => void this.selectEvent(id),
      selectEntity: (id) => void this.selectEntity(id),
      focusRisk: (risk) => void this.focusRisk(risk),
      analyzeEvent: (id) => void this.analyzeEvent(id),
      traceDependencies: (id) => void this.traceDependencies(id),
      simulateFailure: (id) => void this.addFailure(id, 'DOWN'),
      askAbout: (id) => void this.ask(`Tell me about ${store.entityName(id)}`),
      removeOverride: (id) => void this.removeOverride(id),
      generatePlan: () => void this.generatePlan(),
    };

    document.getElementById('app')?.setAttribute('data-view-mode', state.viewMode);
    for (const button of bindAll<HTMLButtonElement>('.mode-switch__button')) {
      button.classList.toggle('is-active', button.dataset['view'] === state.viewMode);
    }

    // -- top bar ---------------------------------------------------------------------
    replaceChildren(bind('headline-stats'), ...renderHeadlineStats(state));
    this.renderWorkspaceSwitcher(state);
    bind('org-name').textContent = state.dashboard?.organization ?? '—';
    bind('run-mode').textContent = state.config?.run_mode ?? 'DEMO';

    // -- graph coverage ---------------------------------------------------------------
    const coverage = renderCoverage(state.workspaceDetail);
    const coveragePanel = bind('coverage-panel');
    // Hidden rather than empty for the demo estate: a fixture that declares everything
    // has no coverage story, and an empty panel invites the reader to wonder what broke.
    coveragePanel.hidden = coverage === null;
    replaceChildren(bind('coverage-body'), ...(coverage ? [coverage] : []));

    // -- notices ---------------------------------------------------------------------
    replaceChildren(
      bind('notices'),
      ...state.notices.map((notice) => {
        const dismiss = el('button', {
          type: 'button',
          class: 'notice__dismiss',
          text: '✕',
          'aria-label': `Dismiss ${notice.title}`,
        });
        dismiss.addEventListener('click', () => store.dismissNotice(notice.id));
        return el(
          'div',
          {
            class: 'notice',
            'data-state': notice.tone === 'error' ? 'UNAVAILABLE' : notice.tone === 'warning' ? 'DEGRADED' : 'REPLAY',
          },
          el('strong', { text: notice.title }),
          el('span', { text: notice.detail }),
          dismiss,
        );
      }),
    );

    // -- left rail -------------------------------------------------------------------
    bind('risk-count').textContent = String(state.risks.length);
    replaceChildren(bind('risk-list'), renderRisks(state, actions));
    bind('event-count').textContent = String(state.events.length);
    replaceChildren(bind('event-list'), renderEvents(state, actions));
    replaceChildren(bind('feed-list'), renderFeeds(state.feeds));

    // -- globe overlays ---------------------------------------------------------------
    const banner = bind('simulation-banner');
    if (state.simulationActive && state.scenario) {
      banner.removeAttribute('hidden');
      bind('simulation-summary').textContent =
        state.scenario.overrides.length === 0
          ? 'No failures added yet'
          : state.scenario.overrides
              .map((o) => `${store.entityName(o.target_id)} ${o.health ?? 'capacity'}`)
              .join(' · ');
    } else {
      banner.setAttribute('hidden', '');
    }

    replaceChildren(bind('legend'), ...this.renderLegend());

    const dependencyToggle = document.querySelector('[data-action="toggle-dependencies"]');
    if (dependencyToggle) {
      dependencyToggle.textContent = `Dependencies: ${state.showDependencies ? 'on' : 'off'}`;
    }

    // -- analyst ----------------------------------------------------------------------
    const analystEngine = bind('analyst-engine');
    analystEngine.textContent = state.config?.analyst.model ?? state.config?.analyst.engine ?? '—';
    analystEngine.setAttribute(
      'title',
      state.config?.analyst.notice ?? 'A language model is configured for this deployment.',
    );

    const transcript = bind('transcript');
    if (state.transcript.length === 0) {
      replaceChildren(
        transcript,
        el('p', {
          class: 'empty-state',
          // Short by design: this sits above the command bar in a scroll region, and the
          // full deterministic-analyst notice is on the engine badge's tooltip.
          text: state.config?.analyst.notice
            ? 'Deterministic analyst — no language model configured. Analysis and simulation are unaffected.'
            : 'Ask about infrastructure, an event, or a what-if scenario. Press / to focus.',
        }),
      );
    } else {
      replaceChildren(
        transcript,
        ...state.transcript.map((message) =>
          el(
            'div',
            { class: `message message--${message.role}` },
            el(
              'div',
              { class: 'message__role' },
              el('span', { text: message.role === 'operator' ? 'You' : message.role === 'system' ? 'WorldGraph' : 'Analyst' }),
              message.engine ? el('span', { text: `· ${message.engine}` }) : null,
            ),
            el('pre', { class: 'message__text', text: message.text }),
            message.tools?.length
              ? el('div', { class: 'message__tools', text: `tools: ${message.tools.join(', ')}` })
              : null,
          ),
        ),
      );
      transcript.scrollTop = transcript.scrollHeight;
    }

    const send = bind<HTMLButtonElement>('command-send');
    send.disabled = store.isBusy('ask');
    send.textContent = store.isBusy('ask') ? '…' : 'Ask';

    replaceChildren(
      bind('suggestions'),
      ...SUGGESTIONS.map((text) => {
        const chip = el('button', { type: 'button', class: 'suggestion', text });
        chip.addEventListener('click', () => void this.ask(text));
        return chip;
      }),
    );

    // -- detail panel ------------------------------------------------------------------
    const detailTitle = bind('detail-title');
    const detailBody = bind('detail-body');
    if (state.plan) {
      detailTitle.textContent = 'Response plan';
      replaceChildren(detailBody, renderPlan(state.plan));
    } else if (state.selectedEvent) {
      detailTitle.textContent = 'Event';
      replaceChildren(detailBody, renderEventDetail(state.selectedEvent, state.analysis, actions));
    } else if (state.selectedEntity) {
      detailTitle.textContent = 'Entity';
      replaceChildren(detailBody, renderEntityDetail(state.selectedEntity, state, actions));
    } else if (state.analysis) {
      detailTitle.textContent = 'Impact analysis';
      replaceChildren(detailBody, renderAnalysis(state.analysis, actions));
    } else {
      detailTitle.textContent = 'Details';
      replaceChildren(
        detailBody,
        el('p', {
          class: 'empty-state',
          text: 'Select an entity or an event on the globe, or pick a scenario from the launcher.',
        }),
      );
    }

    // -- dock ---------------------------------------------------------------------------
    const dockBody = bind('dock-body');
    if (this.dockTab === 'timeline') {
      replaceChildren(dockBody, renderTimeline(state.timeline));
    } else if (this.dockTab === 'simulation') {
      replaceChildren(
        dockBody,
        renderSimulation(
          state,
          actions,
          (entityId, health) => void this.addFailure(entityId, health),
          () => void this.exitSimulation(),
        ),
      );
    } else {
      replaceChildren(dockBody, renderDependencies(state, actions));
    }
  }

  private renderLegend(): HTMLElement[] {
    const items: [string, string][] = [
      ['Healthy', HEALTH_COLORS.HEALTHY],
      ['Degraded', HEALTH_COLORS.DEGRADED],
      ['Down', HEALTH_COLORS.DOWN],
      ['Critical event', SEVERITY_COLORS.CRITICAL],
      ['High event', SEVERITY_COLORS.HIGH],
    ];
    return [
      ...items.map(([label, color]) =>
        el(
          'span',
          { class: 'legend__item' },
          el('span', { class: 'legend__swatch', style: `background:${color}` }),
          el('span', { text: label }),
        ),
      ),
      el('span', {
        class: 'legend__item',
        // Derived, not asserted. The Reality Pass found this line calling an imported
        // Azure subscription synthetic demo data (docs/REALITY_PASS_AUDIT.md, C6).
        text:
          store.activeWorkspace()?.kind === 'DEMO'
            ? 'This estate is synthetic demo data'
            : 'Imported read-only inventory',
      }),
    ];
  }
}

void new WorldGraphApp().start();
