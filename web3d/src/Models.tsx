import { useEffect, useMemo } from 'react';
import * as THREE from 'three';
import type { Layout, Robot, Snapshot } from './types';
import { nodeMap, polygonBounds, toWorld } from './scene-utils';

type SelectableProps = {
  selected?: boolean;
  onSelect?: () => void;
};

function SelectionRing({ radius }: { radius: number }) {
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.045, 0]}>
      <torusGeometry args={[radius, 0.075, 8, 40]} />
      <meshBasicMaterial color="#2563eb" depthWrite={false} />
    </mesh>
  );
}

function Wheel({ position, radius = 0.25 }: { position: [number, number, number]; radius?: number }) {
  return (
    <mesh position={position} rotation={[Math.PI / 2, 0, 0]} castShadow>
      <cylinderGeometry args={[radius, radius, 0.18, 12]} />
      <meshStandardMaterial color="#1e293b" roughness={0.82} />
    </mesh>
  );
}

export function ForkliftModel({
  color,
  selected,
  onSelect,
  load = false,
}: SelectableProps & { color: string; load?: boolean }) {
  return (
    <group onClick={(event) => { event.stopPropagation(); onSelect?.(); }}>
      {selected && <SelectionRing radius={1.75} />}
      <mesh position={[-0.25, 0.42, 0]} castShadow receiveShadow>
        <boxGeometry args={[1.6, 0.62, 1.15]} />
        <meshStandardMaterial color={color} roughness={0.62} />
      </mesh>
      <mesh position={[-0.58, 1.05, 0]} castShadow>
        <boxGeometry args={[0.72, 0.7, 0.88]} />
        <meshStandardMaterial color="#dbeafe" roughness={0.35} metalness={0.05} />
      </mesh>
      {[-0.98, -0.18].map((x) => [-0.51, 0.51].map((z) => <Wheel key={`${x}-${z}`} position={[x, 0.28, z]} />))}
      {[-0.98, -0.2].map((x) => [-0.48, 0.48].map((z) => (
        <mesh key={`post-${x}-${z}`} position={[x, 1.35, z]} castShadow>
          <boxGeometry args={[0.09, 1.85, 0.09]} />
          <meshStandardMaterial color="#334155" roughness={0.55} metalness={0.35} />
        </mesh>
      )))}
      <mesh position={[-0.58, 2.27, 0]} castShadow>
        <boxGeometry args={[0.92, 0.11, 1.12]} />
        <meshStandardMaterial color="#334155" roughness={0.5} metalness={0.35} />
      </mesh>
      {[-0.51, 0.51].map((z) => (
        <mesh key={`mast-${z}`} position={[0.62, 1.15, z]} castShadow>
          <boxGeometry args={[0.12, 2.25, 0.12]} />
          <meshStandardMaterial color="#1e293b" roughness={0.45} metalness={0.45} />
        </mesh>
      ))}
      {[-0.38, 0.38].map((z) => (
        <mesh key={`fork-${z}`} position={[1.38, 0.18, z]} castShadow>
          <boxGeometry args={[1.55, 0.1, 0.13]} />
          <meshStandardMaterial color="#475569" roughness={0.42} metalness={0.52} />
        </mesh>
      ))}
      {load && <PalletLoad position={[1.28, 0.25, 0]} compact />}
    </group>
  );
}

export function CarrierModel({ selected, onSelect, load = false }: SelectableProps & { load?: boolean }) {
  return (
    <group onClick={(event) => { event.stopPropagation(); onSelect?.(); }}>
      {selected && <SelectionRing radius={0.74} />}
      <mesh position={[0, 0.27, 0]} castShadow receiveShadow>
        <boxGeometry args={[0.9, 0.38, 0.68]} />
        <meshStandardMaterial color="#0f9f98" roughness={0.48} />
      </mesh>
      <mesh position={[0.34, 0.48, 0]} castShadow>
        <boxGeometry args={[0.18, 0.11, 0.48]} />
        <meshStandardMaterial color="#ccfbf1" emissive="#0f766e" emissiveIntensity={0.18} />
      </mesh>
      {[-0.28, 0.28].map((x) => [-0.31, 0.31].map((z) => <Wheel key={`${x}-${z}`} position={[x, 0.15, z]} radius={0.11} />))}
      {load && (
        <mesh position={[-0.03, 0.67, 0]} castShadow>
          <boxGeometry args={[0.58, 0.45, 0.48]} />
          <meshStandardMaterial color="#2563eb" roughness={0.72} />
        </mesh>
      )}
    </group>
  );
}

export function HumanModel({ active = false, selected, onSelect }: { active?: boolean } & SelectableProps) {
  return (
    <group onClick={(event) => { event.stopPropagation(); onSelect?.(); }}>
      {selected && <SelectionRing radius={0.48} />}
      <mesh position={[0, 0.92, 0]} castShadow>
        <capsuleGeometry args={[0.18, 0.68, 4, 8]} />
        <meshStandardMaterial color={active ? '#fb923c' : '#64748b'} roughness={0.8} />
      </mesh>
      <mesh position={[0, 1.55, 0]} castShadow>
        <sphereGeometry args={[0.2, 12, 8]} />
        <meshStandardMaterial color="#fed7aa" roughness={0.9} />
      </mesh>
    </group>
  );
}

export function PalletLoad({ position, compact = false }: { position: [number, number, number]; compact?: boolean }) {
  const width = compact ? 0.94 : 1.15;
  const depth = compact ? 0.82 : 0.95;
  return (
    <group position={position}>
      {[-0.32, 0, 0.32].map((z) => (
        <mesh key={z} position={[0, 0.06, z * (depth / 0.82)]} castShadow>
          <boxGeometry args={[width, 0.1, 0.16]} />
          <meshStandardMaterial color="#a16207" roughness={0.9} />
        </mesh>
      ))}
      <mesh position={[0, compact ? 0.42 : 0.48, 0]} castShadow>
        <boxGeometry args={[width * 0.84, compact ? 0.56 : 0.66, depth * 0.78]} />
        <meshStandardMaterial color="#c98b5b" roughness={0.9} />
      </mesh>
      <mesh position={[0, compact ? 0.42 : 0.48, 0]}>
        <boxGeometry args={[width * 0.87, compact ? 0.12 : 0.13, depth * 0.81]} />
        <meshStandardMaterial color="#1d4ed8" roughness={0.62} />
      </mesh>
    </group>
  );
}

export function RobotModels({ layout, snapshot, selected, onSelect }: {
  layout: Layout;
  snapshot: Snapshot;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <>
      {snapshot.robots.filter((robot) => robot.positioned).map((robot) => (
        <group
          key={robot.id}
          position={toWorld(layout, robot.x, robot.y, 0)}
          rotation={[0, robot.th, 0]}
          scale={robot.cls === 'FORKLIFT' ? [0.77, 0.77, 0.77] : [0.82, 0.82, 0.82]}
        >
          {robot.cls === 'FORKLIFT' ? (
            <ForkliftModel color="#2563eb" selected={selected === robot.id} load={robot.load} onSelect={() => onSelect(robot.id)} />
          ) : (
            <CarrierModel selected={selected === robot.id} load={robot.load} onSelect={() => onSelect(robot.id)} />
          )}
        </group>
      ))}
      {snapshot.truth?.forklifts?.map(([id, x, y, heading]) => (
        <group key={id} position={toWorld(layout, x, y, 0)} rotation={[0, heading, 0]} scale={[0.72, 0.72, 0.72]}>
          <ForkliftModel color="#f59e0b" selected={selected === id} onSelect={() => onSelect(id)} />
        </group>
      ))}
      {snapshot.truth?.humans?.map(([id, x, y, , active]) => (
        <group key={id} position={toWorld(layout, x, y, 0)}>
          <HumanModel active={active} selected={selected === id} onSelect={() => onSelect(id)} />
        </group>
      ))}
    </>
  );
}

export function RackAndInventory({ layout, snapshot, visible }: { layout: Layout; snapshot: Snapshot; visible: boolean }) {
  const rackNodes = useMemo(() => nodeMap(layout), [layout]);
  const rackGeometry = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  const cartonGeometry = useMemo(() => new THREE.BoxGeometry(0.44, 0.32, 0.56), []);
  const bandGeometry = useMemo(() => new THREE.BoxGeometry(0.46, 0.07, 0.58), []);
  const palletGeometry = useMemo(() => new THREE.BoxGeometry(0.4, 0.08, 0.11), []);
  const cartonMaterial = useMemo(() => new THREE.MeshStandardMaterial({ color: '#c98b5b', roughness: 0.9 }), []);
  const bandMaterial = useMemo(() => new THREE.MeshStandardMaterial({ color: '#2563eb', roughness: 0.62 }), []);
  const palletMaterial = useMemo(() => new THREE.MeshStandardMaterial({ color: '#a16207', roughness: 0.9 }), []);
  const rackMaterial = useMemo(() => new THREE.MeshStandardMaterial({ color: '#64748b', roughness: 0.56, metalness: 0.42 }), []);
  const inventory = snapshot.site_state.rack ?? {};
  const maxSlots = Math.max(1, snapshot.site_state.slots_per_node ?? 1);
  const inventoryInstances = useMemo(() => {
    const cartons: THREE.Matrix4[] = [];
    const pallets: THREE.Matrix4[] = [];
    const temp = new THREE.Object3D();
    Object.entries(inventory).forEach(([id, quantity]) => {
      const node = rackNodes.get(id);
      const aisle = Number(id.match(/^R(\d+)-/)?.[1] ?? 0) - 1;
      const rack = layout.racks[aisle];
      if (!node || !rack) return;
      const bounds = polygonBounds(rack);
      const columns = 4;
      const levels = 4;
      const count = Math.min(Math.max(0, Math.round(quantity)), maxSlots, columns * levels);
      for (let slot = 0; slot < count; slot += 1) {
        const column = slot % columns;
        const level = Math.floor(slot / columns);
        const x = bounds.minX + (column + 0.5) * (bounds.width / columns);
        const y = node.y;
        const shelfElevation = 0.16 + level * 0.56;
        const elevation = shelfElevation + 0.3;
        temp.position.set(...toWorld(layout, x, y, elevation));
        temp.rotation.set(0, 0, 0);
        temp.scale.set(1, 1, 1);
        temp.updateMatrix();
        cartons.push(temp.matrix.clone());
        [-0.18, 0, 0.18].forEach((offset) => {
          temp.position.set(...toWorld(layout, x, y + offset, shelfElevation + 0.07));
          temp.updateMatrix();
          pallets.push(temp.matrix.clone());
        });
      }
    });
    return { cartons, pallets };
  }, [inventory, layout, maxSlots, rackNodes]);
  const rackInstances = useMemo(() => {
    const result: THREE.Matrix4[] = [];
    const temp = new THREE.Object3D();
    const addBar = (x: number, y: number, elevation: number, width: number, height: number, depth: number) => {
      temp.position.set(...toWorld(layout, x, y, elevation));
      temp.rotation.set(0, 0, 0);
      temp.scale.set(width, height, depth);
      temp.updateMatrix();
      result.push(temp.matrix.clone());
    };
    layout.racks.forEach((rack, rackIndex) => {
      const bounds = polygonBounds(rack);
      const [cx, cy] = bounds.center;
      const divisions = Math.max(1, Math.round(bounds.height / 3.8));
      const storedRackYs = rackIndex < 5
        ? layout.nodes.filter((node) => node.id.startsWith(`R${rackIndex + 1}-`)).map((node) => node.y)
        : [];
      const frameYs = storedRackYs.length
        ? [bounds.minY, ...storedRackYs, bounds.maxY]
        : Array.from({ length: divisions + 1 }, (_, index) => bounds.minY + index * (bounds.height / divisions));
      frameYs.forEach((y) => {
        [bounds.minX, bounds.maxX].forEach((x) => addBar(x, y, 1.45, 0.12, 2.9, 0.12));
        [0.16, 0.72, 1.28, 1.84, 2.5].forEach((elevation) => addBar(cx, y, elevation, bounds.width, 0.1, 0.1));
      });
      [0.16, 0.72, 1.28, 1.84, 2.5].forEach((elevation) => {
        [bounds.minX, bounds.maxX].forEach((x) => addBar(x, cy, elevation, 0.1, 0.1, bounds.height));
      });
    });
    return result;
  }, [layout]);
  useEffect(() => () => {
    rackGeometry.dispose();
    cartonGeometry.dispose();
    bandGeometry.dispose();
    cartonMaterial.dispose();
    bandMaterial.dispose();
    palletGeometry.dispose();
    palletMaterial.dispose();
    rackMaterial.dispose();
  }, [bandGeometry, bandMaterial, cartonGeometry, cartonMaterial, palletGeometry, palletMaterial, rackGeometry, rackMaterial]);
  const inventoryCapacity = Math.max(1, Object.keys(inventory).length * maxSlots);

  return (
    <group visible={visible}>
      <InventoryInstances geometry={rackGeometry} material={rackMaterial} matrices={rackInstances} capacity={rackInstances.length} />
      <InventoryInstances geometry={palletGeometry} material={palletMaterial} matrices={inventoryInstances.pallets} capacity={inventoryCapacity * 3} />
      <InventoryInstances geometry={cartonGeometry} material={cartonMaterial} matrices={inventoryInstances.cartons} capacity={inventoryCapacity} />
      <InventoryInstances geometry={bandGeometry} material={bandMaterial} matrices={inventoryInstances.cartons} capacity={inventoryCapacity} />
    </group>
  );
}

function InventoryInstances({ geometry, material, matrices, capacity }: {
  geometry: THREE.BufferGeometry;
  material: THREE.Material;
  matrices: THREE.Matrix4[];
  capacity: number;
}) {
  const mesh = useMemo(() => {
    const result = new THREE.InstancedMesh(geometry, material, Math.max(1, capacity));
    result.castShadow = true;
    result.receiveShadow = true;
    return result;
  }, [capacity, geometry, material]);
  useEffect(() => {
    matrices.forEach((matrix, index) => mesh.setMatrixAt(index, matrix));
    mesh.count = matrices.length;
    mesh.instanceMatrix.needsUpdate = true;
    mesh.computeBoundingSphere();
  }, [matrices, mesh]);
  useEffect(() => () => { mesh.dispose(); }, [mesh]);
  return <primitive object={mesh} />;
}

export function StagedLoads({ layout, snapshot }: { layout: Layout; snapshot: Snapshot }) {
  const nodes = useMemo(() => nodeMap(layout), [layout]);
  const loads = [
    ...Object.entries(snapshot.site_state.in_stage ?? {}),
    ...Object.entries(snapshot.site_state.out_stage ?? {}),
  ];
  return (
    <>
      {loads.flatMap(([id, count]) => {
        const node = nodes.get(id);
        if (!node) return [];
        return Array.from({ length: Math.max(0, Math.round(count)) }, (_, index) => {
          const row = Math.floor(index / 2);
          const column = index % 2;
          return (
            <PalletLoad
              key={`${id}-${index}`}
              position={toWorld(layout, node.x + column * 1.25 - 0.62, node.y + row * 1.1, 0)}
            />
          );
        });
      })}
    </>
  );
}
