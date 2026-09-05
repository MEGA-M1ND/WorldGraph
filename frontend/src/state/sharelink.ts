/**
 * Shareable incident state.
 *
 * The URL preserves what an operator needs to hand a colleague mid-incident: the event
 * they are looking at, the entity they selected, the camera, the overlays, the simulation,
 * and the lit dependency path.
 *
 * Two policies carried over from God's Eye View's `layerState.js` (MIT), which had them
 * right:
 *
 * * **Hard length ceilings on every untrusted field.** A share link arrives from outside;
 *   an unbounded value in it is either malformed or hostile.
 * * **Reject the whole payload on an unknown or malformed token — never salvage a prefix.**
 *   Half-restoring a share link produces a view that looks deliberate and is not.
 *
 * And one WorldGraph adds: **no secrets, ever.** Only ids and camera numbers go in the URL.
 */

/** Camera position, in degrees and metres. */
export interface CameraState {
  lat: number;
  lon: number;
  height: number;
  heading: number;
  pitch: number;
}

export interface ShareState {
  eventId: string | null;
  entityId: string | null;
  scenarioId: string | null;
  camera: CameraState | null;
  path: string[];
  showDependencies: boolean;
  viewMode: 'executive' | 'engineer';
}

/** Ids are slugs. Anything else is malformed, and ids reach API paths. */
const ID_GRAMMAR = /^[A-Za-z0-9_.:-]{1,192}$/;

const MAX_PATH_HOPS = 24;
const MAX_PARAM_CHARS = 512;

const PARAM = {
  event: 'e',
  entity: 'n',
  scenario: 's',
  camera: 'c',
  path: 'p',
  deps: 'd',
  mode: 'm',
} as const;

/** Encode the shareable slice of state into URL search params. */
export function encodeShareState(state: ShareState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.eventId) params.set(PARAM.event, state.eventId);
  if (state.entityId) params.set(PARAM.entity, state.entityId);
  if (state.scenarioId) params.set(PARAM.scenario, state.scenarioId);
  if (state.camera) {
    const { lat, lon, height, heading, pitch } = state.camera;
    // Fixed precision keeps the URL short and stops a drifting camera from producing a
    // different link on every frame.
    params.set(
      PARAM.camera,
      [
        lat.toFixed(4),
        lon.toFixed(4),
        Math.round(height),
        Math.round(heading),
        Math.round(pitch),
      ].join(','),
    );
  }
  if (state.path.length > 0) params.set(PARAM.path, state.path.slice(0, MAX_PATH_HOPS).join(','));
  if (!state.showDependencies) params.set(PARAM.deps, '0');
  if (state.viewMode === 'engineer') params.set(PARAM.mode, 'eng');
  return params;
}

/**
 * Decode a share link.
 *
 * Returns `null` when any field is malformed. That is deliberate and total: a partially
 * applied share link is a lie about what the author sent.
 */
export function decodeShareState(search: string | URLSearchParams): ShareState | null {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search;

  for (const [, value] of params) {
    if (value.length > MAX_PARAM_CHARS) return null;
  }

  const eventId = readId(params.get(PARAM.event));
  if (eventId === false) return null;
  const entityId = readId(params.get(PARAM.entity));
  if (entityId === false) return null;
  const scenarioId = readId(params.get(PARAM.scenario));
  if (scenarioId === false) return null;

  const camera = readCamera(params.get(PARAM.camera));
  if (camera === false) return null;

  const path = readPath(params.get(PARAM.path));
  if (path === false) return null;

  const depsRaw = params.get(PARAM.deps);
  if (depsRaw !== null && depsRaw !== '0' && depsRaw !== '1') return null;

  const modeRaw = params.get(PARAM.mode);
  if (modeRaw !== null && modeRaw !== 'eng' && modeRaw !== 'exec') return null;

  return {
    eventId,
    entityId,
    scenarioId,
    camera,
    path,
    showDependencies: depsRaw !== '0',
    viewMode: modeRaw === 'eng' ? 'engineer' : 'executive',
  };
}

/** `false` signals malformed; `null` signals absent. */
function readId(raw: string | null): string | null | false {
  if (raw === null) return null;
  return ID_GRAMMAR.test(raw) ? raw : false;
}

function readCamera(raw: string | null): CameraState | null | false {
  if (raw === null) return null;
  const parts = raw.split(',');
  if (parts.length !== 5) return false;
  const [lat, lon, height, heading, pitch] = parts.map(Number);
  if ([lat, lon, height, heading, pitch].some((value) => !Number.isFinite(value))) return false;
  if (lat! < -90 || lat! > 90 || lon! < -180 || lon! > 180) return false;
  // 60,000 km is well beyond geostationary; anything past it is not a camera position.
  if (height! < 0 || height! > 60_000_000) return false;
  if (pitch! < -90 || pitch! > 90) return false;
  return {
    lat: lat!,
    lon: lon!,
    height: height!,
    heading: ((heading! % 360) + 360) % 360,
    pitch: pitch!,
  };
}

function readPath(raw: string | null): string[] | false {
  if (raw === null) return [];
  const hops = raw.split(',');
  if (hops.length > MAX_PATH_HOPS) return false;
  // Every hop must be a valid id. One bad hop rejects the whole path rather than
  // rendering a chain with a gap in it.
  if (!hops.every((hop) => ID_GRAMMAR.test(hop))) return false;
  return hops;
}

/**
 * Write state into the address bar without adding a history entry.
 *
 * `replaceState` rather than `pushState`: the camera moves constantly, and filling the
 * back button with camera positions makes the browser's back button useless.
 */
export function writeShareState(state: ShareState): void {
  const params = encodeShareState(state);
  const query = params.toString();
  const url = `${window.location.pathname}${query ? `?${query}` : ''}`;
  window.history.replaceState(null, '', url);
}

/** Build a full absolute URL for the copy-link button. */
export function shareUrl(state: ShareState): string {
  const params = encodeShareState(state).toString();
  return `${window.location.origin}${window.location.pathname}${params ? `?${params}` : ''}`;
}

/** Read the current address bar, or `null` if it is malformed. */
export function readShareState(): ShareState | null {
  return decodeShareState(window.location.search);
}
