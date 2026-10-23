import { Canvas, useThree } from '@react-three/fiber';
import { useEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import type { SceneProps } from './types';
import { RackAndInventory, RobotModels, StagedLoads } from './Models';
import {
  cameraPosition,
  cameraTarget,
  polygonGeometry,
  polylinePoints,
  routePoints,
  toWorld,
  zoneColor,
} from './scene-utils';

function SceneCamera({
  layout,
  snapshot,
  cameraView,
  cameraRevision,
  reducedMotion,
  selected,
}: Pick<SceneProps, 'layout' | 'snapshot' | 'cameraView' | 'cameraRevision' | 'reducedMotion' | 'selected'>) {
  const { camera, gl } = useThree();
  const { size, invalidate } = useThree();
  const controlsRef = useRef<OrbitControls | null>(null);
  const snapshotRef = useRef(snapshot);
  snapshotRef.current = snapshot;

  useEffect(() => {
    const controls = new OrbitControls(camera, gl.domElement);
    controls.enableDamping = false;
    controls.enablePan = true;
    controls.minZoom = 1.5;
    controls.maxZoom = 90;
    controls.minPolarAngle = 0.42;
    controls.maxPolarAngle = Math.PI / 2.12;
    const render = () => invalidate();
    controls.addEventListener('change', render);
    controlsRef.current = controls;
    return () => {
      controls.removeEventListener('change', render);
      controls.dispose();
      controlsRef.current = null;
    };
  }, [camera, gl, invalidate]);

  useEffect(() => {
    const current = snapshotRef.current;
    const robot = cameraView === 'selected'
      ? current.robots.find((item) => item.id === selected && item.positioned)
      : undefined;
    const selectedTruth = cameraView === 'selected'
      ? current.truth?.forklifts?.find(([id]) => id === selected)
      : undefined;
    const selectedHuman = cameraView === 'selected'
      ? current.truth?.humans?.find(([id]) => id === selected)
      : undefined;
    const view = cameraView === 'selected' ? 'overview' : cameraView;
    const target = selectedTruth
      ? new THREE.Vector3(...toWorld(layout, selectedTruth[1], selectedTruth[2], 0))
      : selectedHuman
        ? new THREE.Vector3(...toWorld(layout, selectedHuman[1], selectedHuman[2], 0))
      : cameraTarget(layout, view, robot);
    const position = cameraPosition(layout, target, cameraView !== 'overview');
    camera.position.copy(position);
    camera.up.set(0, 1, 0);
    if (camera instanceof THREE.OrthographicCamera) {
      const worldWidth = cameraView === 'overview' ? layout.width * 1.22 : cameraView === 'selected' ? 12 : 34;
      const worldHeight = cameraView === 'overview' ? layout.height * 1.55 : cameraView === 'selected' ? 9 : 27;
      camera.zoom = Math.max(1.5, Math.min(size.width / worldWidth, size.height / worldHeight));
      camera.updateProjectionMatrix();
    }
    controlsRef.current?.target.copy(target);
    controlsRef.current?.update();
    invalidate();
  }, [camera, cameraRevision, cameraView, invalidate, layout.height, layout.width, selected, size.height, size.width]);

  return null;
}

function WarehouseFloor({ layout }: Pick<SceneProps, 'layout'>) {
  const floorGeometry = useMemo(() => new THREE.BoxGeometry(layout.width, 0.16, layout.height), [layout.height, layout.width]);
  const outlineGeometry = useMemo(() => new THREE.EdgesGeometry(floorGeometry), [floorGeometry]);
  useEffect(() => () => {
    outlineGeometry.dispose();
    floorGeometry.dispose();
  }, [floorGeometry, outlineGeometry]);
  return (
    <>
      <mesh geometry={floorGeometry} position={[0, -0.08, 0]} receiveShadow>
        <meshStandardMaterial color="#f8fafc" roughness={0.96} />
      </mesh>
      <lineSegments geometry={outlineGeometry} position={[0, 0.012, 0]}>
        <lineBasicMaterial color="#94a3b8" />
      </lineSegments>
    </>
  );
}

function Zones({ layout }: Pick<SceneProps, 'layout'>) {
  const zones = useMemo(() => layout.zones.map((zone) => ({
    ...zone,
    geometry: polygonGeometry(layout, zone.polygon),
    outline: new THREE.BufferGeometry().setFromPoints(polylinePoints(layout, zone.polygon, zone.kind === 'aisle' ? 0.044 : 0.025)),
  })), [layout]);

  useEffect(() => () => {
    zones.forEach((zone) => {
      zone.geometry.dispose();
      zone.outline.dispose();
    });
  }, [zones]);

  return (
    <>
      {zones.map((zone) => (
        <group key={zone.id}>
          {zone.kind !== 'aisle' && (
            <mesh geometry={zone.geometry} position={[0, 0.006, 0]} receiveShadow>
              <meshStandardMaterial color={zoneColor(zone.kind)} transparent opacity={0.7} roughness={1} depthWrite={false} />
            </mesh>
          )}
          <LineObject
            geometry={zone.outline}
            color={zone.kind === 'aisle' ? '#cbd5e1' : '#94a3b8'}
            opacity={zone.kind === 'aisle' ? 0.38 : 0.68}
          />
        </group>
      ))}
    </>
  );
}

function TrafficNetwork({ layout }: Pick<SceneProps, 'layout'>) {
  const geometry = useMemo(() => {
    const nodes = new Map(layout.nodes.map((node) => [node.id, node]));
    const positions: number[] = [];
    layout.edges.forEach((edge) => {
      const start = nodes.get(edge.s);
      const end = nodes.get(edge.e);
      if (!start || !end) return;
      positions.push(...toWorld(layout, start.x, start.y, 0.06), ...toWorld(layout, end.x, end.y, 0.06));
    });
    const result = new THREE.BufferGeometry();
    result.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    return result;
  }, [layout]);

  useEffect(() => () => geometry.dispose(), [geometry]);

  return (
    <lineSegments geometry={geometry}>
      <lineBasicMaterial color="#64748b" transparent opacity={0.42} />
    </lineSegments>
  );
}

function RouteLines({ layout, snapshot, selected }: Pick<SceneProps, 'layout' | 'snapshot' | 'selected'>) {
  const routes = useMemo(() => snapshot.robots.flatMap((robot) => {
    const points = routePoints(layout, robot);
    return points.length > 1 ? [{ id: robot.id, geometry: new THREE.BufferGeometry().setFromPoints(points) }] : [];
  }), [layout, snapshot]);

  useEffect(() => () => routes.forEach(({ geometry }) => geometry.dispose()), [routes]);

  return (
    <>
      {routes.map(({ id, geometry }) => (
        <LineObject
          key={id}
          geometry={geometry}
          color={selected === id ? '#dc2626' : '#2563eb'}
          opacity={selected === id ? 1 : 0.52}
        />
      ))}
    </>
  );
}

function LineObject({ geometry, color, opacity }: { geometry: THREE.BufferGeometry; color: string; opacity: number }) {
  const line = useMemo(() => {
    const material = new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity });
    return new THREE.Line(geometry, material);
  }, [color, geometry, opacity]);
  useEffect(() => () => (line.material as THREE.Material).dispose(), [line]);
  return <primitive object={line} />;
}

function LowWalls({ layout }: Pick<SceneProps, 'layout'>) {
  return (
    <>
      {layout.walls.map((wall, wallIndex) => wall.slice(0, -1).map(([x1, y1], segmentIndex) => {
        const [x2, y2] = wall[segmentIndex + 1];
        const length = Math.hypot(x2 - x1, y2 - y1);
        const angle = Math.atan2(y2 - y1, x2 - x1);
        return (
          <mesh
            key={`${wallIndex}-${segmentIndex}`}
            position={toWorld(layout, (x1 + x2) / 2, (y1 + y2) / 2, 0.25)}
            rotation={[0, angle, 0]}
            castShadow
          >
            <boxGeometry args={[length, 0.5, 0.12]} />
            <meshStandardMaterial color="#94a3b8" roughness={0.72} />
          </mesh>
        );
      }))}
      {layout.doors.map((door) => {
        const [x1, y1] = door.a;
        const [x2, y2] = door.b;
        const length = Math.hypot(x2 - x1, y2 - y1);
        const angle = Math.atan2(y2 - y1, x2 - x1);
        return (
          <group key={door.id} position={toWorld(layout, (x1 + x2) / 2, (y1 + y2) / 2, 0)} rotation={[0, angle, 0]}>
            {[-length / 2, length / 2].map((x) => (
              <mesh key={x} position={[x, 1.2, 0]} castShadow>
                <boxGeometry args={[0.14, 2.4, 0.18]} />
                <meshStandardMaterial color="#475569" metalness={0.3} roughness={0.55} />
              </mesh>
            ))}
            <mesh position={[0, 2.34, 0]} castShadow>
              <boxGeometry args={[length, 0.15, 0.18]} />
              <meshStandardMaterial color="#475569" metalness={0.3} roughness={0.55} />
            </mesh>
          </group>
        );
      })}
    </>
  );
}

function SceneContent(props: SceneProps) {
  return (
    <>
      <color attach="background" args={['#eef3f8']} />
      <ambientLight intensity={1.35} />
      <hemisphereLight args={['#ffffff', '#dbe4ee', 1.55]} />
      <directionalLight
        position={[28, 54, 25]}
        intensity={2.15}
        castShadow
        shadow-mapSize-width={1536}
        shadow-mapSize-height={1536}
        shadow-camera-left={-48}
        shadow-camera-right={48}
        shadow-camera-top={34}
        shadow-camera-bottom={-34}
      />
      <WarehouseFloor layout={props.layout} />
      <Zones layout={props.layout} />
      <TrafficNetwork layout={props.layout} />
      <LowWalls layout={props.layout} />
      <RackAndInventory layout={props.layout} snapshot={props.snapshot} visible={props.showRacks} />
      <StagedLoads layout={props.layout} snapshot={props.snapshot} />
      {props.showRoutes && <RouteLines layout={props.layout} snapshot={props.snapshot} selected={props.selected} />}
      <RobotModels layout={props.layout} snapshot={props.snapshot} selected={props.selected} onSelect={props.onSelect} />
      <SceneCamera
        layout={props.layout}
        snapshot={props.snapshot}
        cameraView={props.cameraView}
        cameraRevision={props.cameraRevision}
        reducedMotion={props.reducedMotion || props.paused}
        selected={props.selected}
      />
    </>
  );
}

export default function WarehouseScene(props: SceneProps) {
  return (
    <Canvas
      orthographic
      frameloop="demand"
      dpr={[1, 1.5]}
      camera={{ position: [58, 68, 55], zoom: 10.2, near: 0.1, far: 240 }}
      shadows
      gl={{ antialias: true, alpha: false, powerPreference: 'high-performance' }}
      onPointerMissed={() => props.onSelect(null)}
    >
      <SceneContent {...props} />
    </Canvas>
  );
}
