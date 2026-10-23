import * as THREE from 'three';
import type { Layout, Point, Robot } from './types';

export type WorldPoint = [number, number, number];

export function toWorld(layout: Layout, x: number, y: number, elevation = 0): WorldPoint {
  return [x - layout.width / 2, elevation, layout.height / 2 - y];
}

export function polygonBounds(polygon: Point[]) {
  const xs = polygon.map(([x]) => x);
  const ys = polygon.map(([, y]) => y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  return {
    minX,
    maxX,
    minY,
    maxY,
    width: maxX - minX,
    height: maxY - minY,
    center: [(minX + maxX) / 2, (minY + maxY) / 2] as Point,
  };
}

export function polygonGeometry(layout: Layout, polygon: Point[]) {
  const shape = new THREE.Shape();
  polygon.forEach(([x, y], index) => {
    const worldX = x - layout.width / 2;
    const shapeY = y - layout.height / 2;
    if (index === 0) shape.moveTo(worldX, shapeY);
    else shape.lineTo(worldX, shapeY);
  });
  shape.closePath();
  const geometry = new THREE.ShapeGeometry(shape);
  geometry.rotateX(-Math.PI / 2);
  return geometry;
}

export function polylinePoints(layout: Layout, polygon: Point[], elevation = 0.025) {
  const closed = polygon.length > 2 ? [...polygon, polygon[0]] : polygon;
  return closed.map(([x, y]) => new THREE.Vector3(...toWorld(layout, x, y, elevation)));
}

export function nodeMap(layout: Layout) {
  return new Map(layout.nodes.map((node) => [node.id, node]));
}

export function routePoints(layout: Layout, robot: Robot) {
  const nodes = nodeMap(layout);
  const ids = [...robot.base, ...robot.route.filter((id) => !robot.base.includes(id))];
  const points = ids
    .map((id) => nodes.get(id))
    .filter((node): node is Layout['nodes'][number] => Boolean(node))
    .map((node) => new THREE.Vector3(...toWorld(layout, node.x, node.y, 0.09)));
  if (points.length && Math.hypot(points[0].x - (robot.x - layout.width / 2), points[0].z - (layout.height / 2 - robot.y)) > 0.2) {
    points.unshift(new THREE.Vector3(...toWorld(layout, robot.x, robot.y, 0.09)));
  }
  return points;
}

export function zoneColor(kind: string) {
  switch (kind) {
    case 'human_work': return '#fef3c7';
    case 'forklift_lane': return '#e0e7ff';
    case 'picking': return '#ccfbf1';
    case 'walkway': return '#f1f5f9';
    case 'charging': return '#dbeafe';
    case 'aisle': return '#f8fafc';
    default: return '#f1f5f9';
  }
}

export function cameraTarget(layout: Layout, view: 'overview' | 'inbound' | 'racks' | 'outbound', selected?: Robot) {
  if (selected) return new THREE.Vector3(...toWorld(layout, selected.x, selected.y, 0));
  if (view === 'inbound') return new THREE.Vector3(-layout.width * 0.38, 0, 1);
  if (view === 'outbound') return new THREE.Vector3(layout.width * 0.38, 0, 1);
  if (view === 'racks') return new THREE.Vector3(-layout.width * 0.15, 0, 1);
  return new THREE.Vector3(0, 0, 0);
}

export function cameraPosition(layout: Layout, target: THREE.Vector3) {
  const span = Math.max(layout.width, layout.height);
  const elevation = span * 1.2;
  const tilt = THREE.MathUtils.degToRad(18);
  return target.clone().add(new THREE.Vector3(0, elevation, elevation * Math.tan(tilt)));
}
