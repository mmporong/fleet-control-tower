export type Point = [number, number];
export interface Layout {
  width: number; height: number; site: string;
  nodes: {id: string; x: number; y: number; kind: string}[];
  edges: {id: string; s: string; e: string; oneway: boolean}[];
  racks: Point[][]; walls: Point[][];
  zones: {id: string; name: string; kind: string; polygon: Point[]; static_speed: number | null}[];
  doors: {id: string; name: string; a: Point; b: Point}[];
}
export interface Robot {
  id: string; cls: string; x: number; y: number; th: number; v: number;
  len: number; wid: number; st: string; label: string; bat: number;
  load: boolean; conn: string; purpose: string; goal: string | null;
  base: string[]; route: string[]; positioned: boolean;
}
export interface Snapshot {
  t: number; scenario: string; scenario_title: string; layout_key: string; mode: string;
  robots: Robot[];
  truth?: {forklifts: [string,number,number,number,number][]; humans: [string,number,number,string,boolean,number][]; external: [string,number,number,number][]};
  site_state: {rack?: Record<string,number>; slots_per_node?: number; in_stage?: Record<string,number>; out_stage?: Record<string,number>; lane_capacity?: number; trucks_waiting?: number};
  alarm_counts: {p1: number;p2: number;p3: number};
  kpi: {working: number; queued: number; done: number; site?: {rack_fill: number; staged_in: number; staged_out: number}};
  run?: {read_only: boolean; paused: boolean; speed: number};
}
export interface SceneProps {
  layout: Layout; snapshot: Snapshot; selected: string | null; onSelect: (id: string | null) => void;
  cameraView: 'overview' | 'inbound' | 'racks' | 'outbound' | 'selected'; cameraRevision: number;
  showRoutes: boolean; showRacks: boolean; paused: boolean; reducedMotion: boolean;
}
