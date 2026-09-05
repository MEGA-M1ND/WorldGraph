/**
 * Cesium viewer setup.
 *
 * Two decisions carried from the baseline audit:
 *
 * * **Chrome off.** WorldGraph is an operations console, not GIS software; every default
 *   Cesium widget is disabled and the UI provides what it needs.
 * * **`requestRenderMode` on, with an explicit governor.** God's Eye View measured
 *   32.4 ms/frame for 58 ground primitives with per-frame callback geometry versus
 *   1.4 ms/frame with static geometry. WorldGraph therefore renders on demand and holds
 *   continuous rendering *only* while an animation is actually running.
 *
 * And one WorldGraph adds: **it boots with no credentials.** Cesium Ion and Google
 * Photorealistic 3D Tiles are opt-in via the backend's config endpoint. Without them the
 * globe renders from Cesium's bundled Natural Earth II imagery, which needs no account —
 * making the demo reproducible on any machine.
 */

import {
  Cartesian3,
  Cartographic,
  Color,
  Ion,
  Math as CesiumMath,
  Rectangle,
  ScreenSpaceEventType,
  ShadowMode,
  TileMapServiceImageryProvider,
  Viewer,
  buildModuleUrl,
} from 'cesium';

import 'cesium/Build/Cesium/Widgets/widgets.css';

import type { CameraState } from '../state/sharelink.ts';

/** Opening view: the whole world, tilted enough to read as a globe rather than a map. */
const HOME_VIEW = {
  destination: Cartesian3.fromDegrees(60.0, 12.0, 26_000_000),
};

export interface GlobeHandles {
  viewer: Viewer;
  /** Request a single frame. Call after any discrete change. */
  requestRender: () => void;
  /** Hold continuous rendering for an animation; the returned function releases it. */
  holdContinuousRender: () => () => void;
  flyTo: (lat: number, lon: number, height?: number) => Promise<void>;
  flyToEntities: (positions: { lat: number; lon: number }[]) => Promise<void>;
  cameraState: () => CameraState;
  setCameraState: (camera: CameraState) => void;
  destroy: () => void;
}

export interface GlobeOptions {
  container: HTMLElement;
  cesiumIonToken?: string | null;
  onCameraIdle?: (camera: CameraState) => void;
}

export async function createGlobe(options: GlobeOptions): Promise<GlobeHandles> {
  const { container, cesiumIonToken, onCameraIdle } = options;

  // Only set the token when one was actually supplied. Setting it to an empty string
  // makes Cesium attempt authenticated asset requests that then 401 on every tile.
  if (cesiumIonToken) {
    Ion.defaultAccessToken = cesiumIonToken;
  }

  const viewer = new Viewer(container, {
    // Chrome off — the UI supplies its own controls.
    timeline: false,
    animation: false,
    baseLayerPicker: false,
    geocoder: false,
    homeButton: false,
    sceneModePicker: false,
    navigationHelpButton: false,
    fullscreenButton: false,
    vrButton: false,
    selectionIndicator: false,
    infoBox: false,
    // Render only when something changed. Everything below that mutates the scene calls
    // requestRender(); only genuine animations hold it continuous.
    requestRenderMode: true,
    maximumRenderTimeChange: Infinity,
    baseLayer: false,
    msaaSamples: 4,
    contextOptions: { webgl: { alpha: false, powerPreference: 'high-performance' } },
  });

  // Bundled offline imagery: no key, no network, works on a plane.
  try {
    viewer.imageryLayers.addImageryProvider(
      await TileMapServiceImageryProvider.fromUrl(
        buildModuleUrl('Assets/Textures/NaturalEarthII'),
      ),
    );
  } catch {
    // Imagery is presentation, not function. A globe with no texture still shows the
    // estate and every dependency path, so this must never block startup.
  }

  const { scene } = viewer;
  scene.globe.enableLighting = false;
  scene.globe.showGroundAtmosphere = true;
  scene.globe.baseColor = Color.fromCssColorString('#0a0e18');
  if (scene.skyAtmosphere) {
    // Desaturated and dimmed: the default atmosphere is a bright blue halo that competes
    // with the severity colours, which are the only thing on this globe allowed to be loud.
    scene.skyAtmosphere.hueShift = -0.02;
    scene.skyAtmosphere.saturationShift = -0.25;
    scene.skyAtmosphere.brightnessShift = -0.18;
  }
  scene.backgroundColor = Color.fromCssColorString('#05070d');
  scene.fog.enabled = true;
  scene.shadowMap.enabled = false;
  scene.globe.shadows = ShadowMode.DISABLED;
  // Depth testing against terrain would hide the dependency arcs that are the point of
  // this product; WorldGraph draws its graph above the surface deliberately.
  scene.globe.depthTestAgainstTerrain = false;
  viewer.cesiumWidget.creditContainer.classList.add('wg-credits');

  // Cesium's own click-to-select fights our picking; the entity layer owns selection.
  viewer.screenSpaceEventHandler.removeInputAction(ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

  viewer.camera.setView(HOME_VIEW);

  let continuousHolders = 0;
  const requestRender = () => viewer.scene.requestRender();
  const holdContinuousRender = () => {
    continuousHolders += 1;
    viewer.scene.requestRenderMode = false;
    let released = false;
    return () => {
      if (released) return;
      released = true;
      continuousHolders -= 1;
      if (continuousHolders <= 0) {
        continuousHolders = 0;
        viewer.scene.requestRenderMode = true;
        viewer.scene.requestRender();
      }
    };
  };

  const cameraState = (): CameraState => {
    const { camera } = viewer;
    const carto = Cartographic.fromCartesian(camera.positionWC);
    return {
      lat: CesiumMath.toDegrees(carto.latitude),
      lon: CesiumMath.toDegrees(carto.longitude),
      height: carto.height,
      heading: CesiumMath.toDegrees(camera.heading),
      pitch: CesiumMath.toDegrees(camera.pitch),
    };
  };

  if (onCameraIdle) {
    // moveEnd rather than a per-frame listener: the share link only needs the camera
    // where it came to rest, and writing the URL every frame is pure waste.
    viewer.camera.moveEnd.addEventListener(() => onCameraIdle(cameraState()));
  }

  const flyTo = (lat: number, lon: number, height = 900_000) =>
    new Promise<void>((resolve) => {
      const release = holdContinuousRender();
      viewer.camera.flyTo({
        destination: Cartesian3.fromDegrees(lon, lat, height),
        orientation: { heading: 0, pitch: CesiumMath.toRadians(-55), roll: 0 },
        duration: 1.8,
        complete: () => {
          release();
          resolve();
        },
        cancel: () => {
          release();
          resolve();
        },
      });
    });

  const flyToEntities = (positions: { lat: number; lon: number }[]) => {
    if (positions.length === 0) return Promise.resolve();
    if (positions.length === 1) {
      const only = positions[0]!;
      return flyTo(only.lat, only.lon, 1_400_000);
    }
    const lats = positions.map((p) => p.lat);
    const lons = positions.map((p) => p.lon);
    const lonSpan = Math.max(...lons) - Math.min(...lons);

    // A blast radius that reaches customers on three continents produces a bounding box
    // spanning most of the planet, and framing it puts the camera over the middle of the
    // Atlantic with every entity too small to see. Past this span, frame the first
    // position instead — it is the origin, which is where the story starts.
    if (lonSpan > 150) {
      const anchor = positions[0]!;
      return flyTo(anchor.lat, anchor.lon, 6_000_000);
    }

    // Pad the bounding box so the outermost entities are not pinned against the edge of
    // the viewport, where a marker is easy to miss.
    const rectangle = Rectangle.fromDegrees(
      Math.min(...lons) - 6,
      Math.min(...lats) - 6,
      Math.max(...lons) + 6,
      Math.max(...lats) + 6,
    );
    return new Promise<void>((resolve) => {
      const release = holdContinuousRender();
      viewer.camera.flyTo({
        destination: rectangle,
        duration: 1.8,
        complete: () => {
          release();
          resolve();
        },
        cancel: () => {
          release();
          resolve();
        },
      });
    });
  };

  const setCameraState = (camera: CameraState) => {
    viewer.camera.setView({
      destination: Cartesian3.fromDegrees(camera.lon, camera.lat, camera.height),
      orientation: {
        heading: CesiumMath.toRadians(camera.heading),
        pitch: CesiumMath.toRadians(camera.pitch),
        roll: 0,
      },
    });
    requestRender();
  };

  return {
    viewer,
    requestRender,
    holdContinuousRender,
    flyTo,
    flyToEntities,
    cameraState,
    setCameraState,
    destroy: () => {
      if (!viewer.isDestroyed()) viewer.destroy();
    },
  };
}
