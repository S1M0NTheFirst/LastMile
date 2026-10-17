"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { FLOOR_DATA, CENTER_U, DOORS, doorFeatures } from "./lib/floor4";

const FINAL = ["completed", "failed", "cancelled", "reassigned"];
const COLORS = { alive: 0x45d5a2, no_response: 0xffc06b, dead: 0xff7684, off: 0x5b636f, busy: 0x6aaaff };
const TRAIL_MAX = 300;

// Fleet status as shown in the UI: off / no_response / dead / alive.
function robotStatus(r) {
  if (r.power === "off") return "off";
  if (!r.reachable) return "no_response";
  return r.state?.status || "alive";
}

function statusClass(s) {
  return { alive: "green", no_response: "orange", dead: "red", off: "grey" }[s] || "blue";
}

function robotHex(r) {
  const s = robotStatus(r);
  if (s === "alive" && r.state?.mode && r.state.mode !== "idle") return COLORS.busy;
  return COLORS[s] ?? COLORS.alive;
}

// ---------------------------------------------------------------------------
// Campus world rendering. Units are meters with the ECS building's centroid at
// the origin; +x east, +z south (so north is -z).
// ---------------------------------------------------------------------------

const WORLD_RADIUS = 200;

const PALETTE = {
  ground: 0x1a2a37,
  grass: 0x24493a,
  parking: 0x2a333d,
  plaza: 0x3a4a58,
  water: 0x1f4f7a,
  road: 0x2e3944,
  service: 0x35414d,
  footway: 0x5a6e7f,
  cycleway: 0x4f6b6a,
  steps: 0x6f8191,
  building: 0x51677c,
  anchor: 0x45d5a2,
  edge: 0x8fb3cc,
  trunk: 0x4a3a2c,
  crown: 0x2f6b47,
  entrance: 0xffc06b,
  perimeter: 0x45d5a2,
};

// A shape in the XZ plane. Shape space uses (x, -z) so that after rotateX(-90°)
// the points land back on scene (x, z).
function shapeFrom(outer, holes = []) {
  const shape = new THREE.Shape(outer.map(([x, z]) => new THREE.Vector2(x, -z)));
  for (const h of holes) shape.holes.push(new THREE.Path(h.map(([x, z]) => new THREE.Vector2(x, -z))));
  return shape;
}

function flatArea(scene, outer, holes, color, y) {
  const geo = new THREE.ShapeGeometry(shapeFrom(outer, holes));
  geo.rotateX(-Math.PI / 2);
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.95, side: THREE.DoubleSide }));
  mesh.position.y = y;
  scene.add(mesh);
  return mesh;
}

// All segments of one path kind become a single flat ribbon mesh with round-ish joints.
function ribbons(scene, paths, color, y) {
  if (!paths.length) return;
  const pos = [];
  const quad = (a, b, c, d) => pos.push(...a, ...b, ...c, ...a, ...c, ...d);
  for (const p of paths) {
    const w = p.width / 2;
    for (let i = 1; i < p.points.length; i++) {
      const [x1, z1] = p.points[i - 1], [x2, z2] = p.points[i];
      const len = Math.hypot(x2 - x1, z2 - z1);
      if (len < 1e-3) continue;
      const nx = (-(z2 - z1) / len) * w, nz = ((x2 - x1) / len) * w;
      quad([x1 + nx, y, z1 + nz], [x1 - nx, y, z1 - nz], [x2 - nx, y, z2 - nz], [x2 + nx, y, z2 + nz]);
    }
    // Joint discs so bends don't show notches.
    for (const [x, z] of p.points) {
      const n = 10;
      for (let k = 0; k < n; k++) {
        const a0 = (k / n) * Math.PI * 2, a1 = ((k + 1) / n) * Math.PI * 2;
        pos.push(x, y, z, x + Math.cos(a1) * w, y, z + Math.sin(a1) * w, x + Math.cos(a0) * w, y, z + Math.sin(a0) * w);
      }
    }
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  geo.computeVertexNormals();
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color, roughness: 0.9, side: THREE.DoubleSide }));
  scene.add(mesh);
}

function label(scene, text, x, y, z, color = "#e9f4ff", scale = 1) {
  const c = document.createElement("canvas");
  const ctx = c.getContext("2d");
  const font = "bold 40px Arial, sans-serif";
  ctx.font = font;
  c.width = Math.ceil(ctx.measureText(text).width) + 36;
  c.height = 64;
  ctx.font = font;
  ctx.fillStyle = "rgba(7, 22, 36, 0.82)";
  ctx.beginPath();
  ctx.roundRect(0, 0, c.width, c.height, 12);
  ctx.fill();
  ctx.fillStyle = color;
  ctx.textBaseline = "middle";
  ctx.fillText(text, 18, 34);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true, fog: false }));
  const h = 3.2 * scale;
  sprite.scale.set((c.width / c.height) * h, h, 1);
  sprite.position.set(x, y, z);
  sprite.renderOrder = 10;
  scene.add(sprite);
  return sprite;
}

function centroid(ring) {
  let a = 0, cx = 0, cz = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const f = ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
    a += f;
    cx += (ring[j][0] + ring[i][0]) * f;
    cz += (ring[j][1] + ring[i][1]) * f;
  }
  return Math.abs(a) < 1e-9 ? ring[0] : [cx / (3 * a), cz / (3 * a)];
}

function ringArea2(r) {
  let a = 0;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) a += (r[j][0] + r[i][0]) * (r[j][1] - r[i][1]);
  return a / 2;
}

function groundAndPerimeter(scene) {
  const ground = new THREE.Mesh(
    new THREE.CylinderGeometry(WORLD_RADIUS, WORLD_RADIUS, 0.6, 128),
    new THREE.MeshStandardMaterial({ color: PALETTE.ground, roughness: 1 })
  );
  ground.position.y = -0.3;
  scene.add(ground);

  const ring = new THREE.Mesh(
    new THREE.TorusGeometry(WORLD_RADIUS, 0.25, 8, 192),
    new THREE.MeshBasicMaterial({ color: PALETTE.perimeter, transparent: true, opacity: 0.85 })
  );
  ring.rotation.x = Math.PI / 2;
  ring.position.y = 0.05;
  scene.add(ring);

  // 25 m distance rings from ECS, faint, for orientation.
  for (let r = 25; r < WORLD_RADIUS; r += 25) {
    const pts = Array.from({ length: 129 }, (_, i) => {
      const t = (i / 128) * Math.PI * 2;
      return new THREE.Vector3(Math.cos(t) * r, 0.07, Math.sin(t) * r);
    });
    scene.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: 0x3d5a70, transparent: true, opacity: 0.35 })));
  }
  label(scene, "N", 0, 1.5, -WORLD_RADIUS - 5, "#45d5a2", 1.4);
}

function buildCampusWorld(scene, layout) {

  // Ground cover
  layout.areas.forEach((a, i) => {
    const color = PALETTE[a.kind] ?? PALETTE.plaza;
    flatArea(scene, a.outer, a.holes, color, 0.01 + i * 0.0005);
  });

  // Paths and roads, wider surfaces lower so walkways sit on top of roads.
  const byKind = (k) => layout.paths.filter((p) => p.kind === k);
  ribbons(scene, byKind("road"), PALETTE.road, 0.12);
  ribbons(scene, byKind("service"), PALETTE.service, 0.13);
  ribbons(scene, byKind("cycleway"), PALETTE.cycleway, 0.14);
  ribbons(scene, byKind("footway"), PALETTE.footway, 0.15);
  ribbons(scene, byKind("steps"), PALETTE.steps, 0.16);

  // Buildings: footprints extruded to their OSM height.
  const edgeMat = new THREE.LineBasicMaterial({ color: PALETTE.edge, transparent: true, opacity: 0.55 });
  const labelled = new Set();
  let anchor = null;
  const anchorMeshes = [];
  for (const b of layout.buildings) {
    // ECS has rooms numbered 5xx (e.g. ECS-552), so it is at least five storeys. If OSM has no
    // height for it, don't let the 12 m default cut the building off below the 4th floor.
    if (b.anchor && b.heightSource === "default") b.height = Math.max(b.height, 5 * 3.66 + 1);
    const depth = Math.max(1, b.height - (b.minHeight || 0));
    const geo = new THREE.ExtrudeGeometry(shapeFrom(b.outer, b.holes), { depth, bevelEnabled: false });
    geo.rotateX(-Math.PI / 2);
    const color = b.anchor ? PALETTE.anchor : PALETTE.building;
    const mesh = new THREE.Mesh(
      geo,
      new THREE.MeshStandardMaterial({
        color, roughness: 0.6, metalness: 0.08,
        transparent: b.anchor, opacity: b.anchor ? 0.55 : 1,
        emissive: b.anchor ? PALETTE.anchor : 0x000000, emissiveIntensity: b.anchor ? 0.12 : 0,
      })
    );
    mesh.position.y = b.minHeight || 0;
    mesh.userData.building = b;
    scene.add(mesh);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 25), b.anchor ? edgeMat.clone() : edgeMat);
    edges.position.y = mesh.position.y;
    scene.add(edges);
    if (b.anchor) anchorMeshes.push(mesh, edges);

    if (b.anchor && (!anchor || Math.abs(ringArea2(b.outer)) > Math.abs(ringArea2(anchor.outer)))) anchor = b;
    const name = b.ref || b.name;
    if (name && !labelled.has(b.id)) {
      labelled.add(b.id);
      const [cx, cz] = centroid(b.outer);
      label(scene, b.ref && b.name ? `${b.ref} · ${b.name}` : name, cx, b.height + 3, cz, b.anchor ? "#45d5a2" : "#e9f4ff", b.anchor ? 1.4 : 1);
    }
  }

  // Trees as two instanced meshes (trunks + crowns).
  if (layout.trees.length) {
    const n = layout.trees.length;
    const trunks = new THREE.InstancedMesh(new THREE.CylinderGeometry(0.2, 0.28, 3, 6), new THREE.MeshStandardMaterial({ color: PALETTE.trunk }), n);
    const crowns = new THREE.InstancedMesh(new THREE.IcosahedronGeometry(2.4, 1), new THREE.MeshStandardMaterial({ color: PALETTE.crown, roughness: 0.9, flatShading: true }), n);
    const m = new THREE.Matrix4();
    layout.trees.forEach(([x, z], i) => {
      const s = 0.85 + ((Math.sin(x * 12.9898 + z * 78.233) * 43758.5453) % 1 + 1) % 1 * 0.4; // stable per-tree size
      trunks.setMatrixAt(i, m.makeScale(1, s, 1).setPosition(x, 1.5 * s, z));
      crowns.setMatrixAt(i, m.makeScale(s, s, s).setPosition(x, 3.6 * s, z));
    });
    scene.add(trunks, crowns);
  }

  // Building entrances as small beacons.
  for (const e of layout.entrances) {
    const pole = new THREE.Mesh(
      new THREE.CylinderGeometry(0.18, 0.18, 2.2, 8),
      new THREE.MeshStandardMaterial({ color: PALETTE.entrance, emissive: PALETTE.entrance, emissiveIntensity: 0.6 })
    );
    pole.position.set(e.x, 1.1, e.z);
    scene.add(pole);
    if (e.name) label(scene, e.name, e.x, 3.4, e.z, "#ffc06b", 0.6);
  }

  return { anchor, anchorMeshes, source: layout.source, generated: layout.generated };
}

// Shown while the map loads or if it fails: a marker for ECS only.
// Deliberately no invented buildings, so nothing here can be mistaken for the real layout.
function buildPlaceholderWorld(scene) {
  const box = new THREE.Mesh(
    new THREE.BoxGeometry(30, 18, 30),
    new THREE.MeshStandardMaterial({ color: PALETTE.anchor, transparent: true, opacity: 0.25, emissive: PALETTE.anchor, emissiveIntensity: 0.1 })
  );
  box.position.y = 9;
  scene.add(box);
  scene.add(new THREE.LineSegments(new THREE.EdgesGeometry(box.geometry), new THREE.LineBasicMaterial({ color: PALETTE.anchor })).translateY(9));
  label(scene, "ECS (loading campus map…)", 0, 22, 0, "#45d5a2", 1.2);
  return { anchor: null, source: null };
}

function disposeScene(scene) {
  scene.traverse((o) => {
    o.geometry?.dispose?.();
    const mats = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
    for (const m of mats) {
      m.map?.dispose?.();
      m.dispose?.();
    }
  });
}


// ---------------------------------------------------------------------------
// ECS 4th floor (hallways only), reconstructed from three photos plus measured lengths.
//
// Plan coordinates, in meters:
//   u = forward along the main hallway, 0 = where Hallway.jpg was taken
//   v = to the right when looking down the main hallway
// The main hallway runs into a T junction with a cross hallway; Left_Hallway.jpg and
// Right_Hallway.jpg look down its two branches.
//
// Door centers use the measured step counts in robot/floor4.json. Steps are provisionally
// centerline distances from the junction near 418. Widths still come from the earlier plan.
// Bathroom locations remain unplaced until their branch and wall side are confirmed.
// ---------------------------------------------------------------------------
const FLOOR4 = {
  elevation: 3 * 3.66, // floor 4 = three typical 3.66 m (12 ft) storeys above ground level
  // Which way round the floor goes. The footprint alone can't tell whether the main hallway
  // points one way or the opposite way, so after the automatic fit this many extra half-turns
  // are applied. 0 matches the real building as checked on site: looking down the main
  // hallway (as in Hallway.jpg) faces the ECS entrance. Set to 1 to turn the floor 180°.
  halfTurns: 0,
  wallHeight: 2.75,
  stemHalfWidth: 0.965, // previous measured width; not rescaled with the corrected tile size
  stemStart: -1.0, // the hallway behind the camera wasn't photographed; assumed to end 1 m back
  junctionNear: FLOOR_DATA.geometry.junctionNear,
  junctionFar: FLOOR_DATA.geometry.junctionFar,
  leftEnd: FLOOR_DATA.geometry.leftEnd, // allow clearance beyond the last measured door
  rightEnd: FLOOR_DATA.geometry.rightEnd,
  alcove: { v0: -3.14, v1: -1.2, depth: 0.33 }, // water-fountain recess in the far wall
  // Wall-mounted features. "wall" names a wall below; a0..a1 is the span along it
  // (u for main-hallway walls, v for cross-hallway walls). "label" is a name for reference
  // in this table only; nothing is drawn for it in the 3D view.
  features: [
    { wall: "stemLeft", a0: 0.0, a1: 2.4, kind: "board", label: null },
    { wall: "stemLeft", a0: 3.6, a1: 4.74, kind: "double", label: "Double doors" },
    { wall: "stemLeft", a0: 5.36, a1: 7.0, kind: "case", label: null },
    { wall: "stemRight", a0: 2.65, a1: 3.75, kind: "elevator", label: "Elevator" },
    { wall: "stemRight", a0: 4.55, a1: 5.45, kind: "elevator", label: "Elevator" },
    { wall: "alcoveBack", a0: -2.55, a1: -1.65, kind: "fountain", label: "Water fountain" },
    { wall: "endLeft", a0: 7.5, a1: 8.35, kind: "door", label: "Stairs (exit)", exit: true },
    { wall: "endRight", a0: 7.25, a1: 8.6, kind: "glass", label: "Exit to outside" },
  ].concat(doorFeatures()),
  // Free-standing objects: plan position and footprint size.
  objects: [
    { u: 6.1, v: 0.7, w: 0.45, d: 0.35, h: 0.85, color: 0x1f4fa8, label: "Recycling" },
    { u: 8.4, v: -26.6, w: 0.45, d: 0.4, h: 0.9, color: 0x15191e, label: null },
    { u: 7.45, v: 27.0, w: 0.45, d: 0.4, h: 0.9, color: 0x15191e, label: null },
  ],
};

// The walls of the T, each with the direction (in plan coordinates) its corridor-side face points.
function floor4Walls(F) {
  const { stemHalfWidth: s, stemStart: u0, junctionNear: uj, junctionFar: uf, leftEnd: vl, rightEnd: vr, alcove: a } = F;
  // Along-u walls: { u0, u1, v, n: +1/-1 normal in v }. Along-v walls: { v0, v1, u, n: normal in u }.
  return {
    stemLeft: { axis: "u", a0: u0, a1: uj, at: -s, n: 1 },
    stemRight: { axis: "u", a0: u0, a1: uj, at: s, n: -1 },
    stemBack: { axis: "v", a0: -s, a1: s, at: u0, n: 1 },
    nearLeft: { axis: "v", a0: vl, a1: -s, at: uj, n: 1 },
    nearRight: { axis: "v", a0: s, a1: vr, at: uj, n: 1 },
    far: { axis: "v", a0: vl, a1: vr, at: uf, n: -1, gap: [a.v0, a.v1] },
    alcoveBack: { axis: "v", a0: a.v0, a1: a.v1, at: uf + a.depth, n: -1 },
    alcoveL: { axis: "u", a0: uf, a1: uf + a.depth, at: a.v0, n: 1 },
    alcoveR: { axis: "u", a0: uf, a1: uf + a.depth, at: a.v1, n: -1 },
    endLeft: { axis: "u", a0: uj, a1: uf, at: vl, n: 1 },
    endRight: { axis: "u", a0: uj, a1: uf, at: vr, n: -1 },
  };
}

// Plan (u, v) → floor-group local (x, z): v is +x, forward u is -z.
const planToLocal = (u, v) => [v, -u];

function tileTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 64;
  const g = c.getContext("2d");
  g.fillStyle = "#e4e2dc";
  g.fillRect(0, 0, 64, 64);
  for (let i = 0; i < 90; i++) {
    g.fillStyle = `rgba(90,90,90,${0.08 + Math.random() * 0.12})`;
    g.fillRect(Math.random() * 64, Math.random() * 64, 1.5, 1.5);
  }
  g.strokeStyle = "rgba(60,60,60,0.45)";
  g.lineWidth = 1.5;
  g.strokeRect(0, 0, 64, 64);
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

// Builds the hallway interior in floor-local coordinates.
function buildFloor4(F = FLOOR4) {
  const group = new THREE.Group();
  const occluders = [];
  const walls = floor4Walls(F);
  const H = F.wallHeight;
  const TILE = FLOOR_DATA.tileMeters;

  const mat = {
    wall: new THREE.MeshStandardMaterial({ color: 0xf1ede4, roughness: 0.85 }),
    base: new THREE.MeshStandardMaterial({ color: 0x1d1b1a, roughness: 0.6 }),
    door: new THREE.MeshStandardMaterial({ color: 0xb8a296, roughness: 0.7 }),
    frame: new THREE.MeshStandardMaterial({ color: 0xa48d80, roughness: 0.7 }),
    kick: new THREE.MeshStandardMaterial({ color: 0x2b2826, roughness: 0.5 }),
    steel: new THREE.MeshStandardMaterial({ color: 0x9aa1a6, roughness: 0.35, metalness: 0.6 }),
    glass: new THREE.MeshStandardMaterial({ color: 0x9fd4ff, transparent: true, opacity: 0.35, roughness: 0.1 }),
    cork: new THREE.MeshStandardMaterial({ color: 0xc7a878, roughness: 0.9 }),
    wood: new THREE.MeshStandardMaterial({ color: 0xa8743d, roughness: 0.6 }),
  };

  const box = (w, h, d, m, x, y, z, rotY = 0) => {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m.clone());
    mesh.userData.originalOpacity = m.opacity;
    mesh.userData.originalTransparent = m.transparent;
    occluders.push(mesh);
    mesh.position.set(x, y, z);
    mesh.rotation.y = rotY;
    group.add(mesh);
    return mesh;
  };

  // Floor slabs with the measured 25 cm tiles.
  const tex = tileTexture();
  const slab = (u0, u1, v0, v1) => {
    const w = v1 - v0, d = u1 - u0;
    const t = tex.clone();
    t.needsUpdate = true;
    t.repeat.set(w / TILE, d / TILE);
    const [x, z] = planToLocal((u0 + u1) / 2, (v0 + v1) / 2);
    box(w, 0.06, d, new THREE.MeshStandardMaterial({ map: t, roughness: 0.25, metalness: 0.05 }), x, -0.03, z);
  };
  slab(F.stemStart, F.junctionNear, -F.stemHalfWidth, F.stemHalfWidth);
  slab(F.junctionNear, F.junctionFar, F.leftEnd, F.rightEnd);
  slab(F.junctionFar, F.junctionFar + F.alcove.depth, F.alcove.v0, F.alcove.v1);

  // Walls (12 cm thick, set behind the corridor face) with black rubber baseboards.
  const T = 0.12;
  const wallSpan = (wl, a0, a1) => {
    const len = a1 - a0;
    if (len <= 0.01) return;
    const mid = (a0 + a1) / 2;
    const off = -wl.n * (T / 2); // push the wall body away from the corridor
    let x, z, rot;
    if (wl.axis === "u") {
      [x, z] = planToLocal(mid, wl.at + off);
      rot = Math.PI / 2;
    } else {
      [x, z] = planToLocal(wl.at + off, mid);
      rot = 0;
    }
    box(len + (wl.axis === "u" ? 0 : T), H, T, mat.wall, x, H / 2, z, rot);
    const [bx, bz] = wl.axis === "u" ? planToLocal(mid, wl.at + wl.n * 0.006) : planToLocal(wl.at + wl.n * 0.006, mid);
    box(len, 0.1, 0.012, mat.base, bx, 0.05, bz, rot);
  };
  for (const wl of Object.values(walls)) {
    if (wl.gap) {
      wallSpan(wl, wl.a0, wl.gap[0]);
      wallSpan(wl, wl.gap[1], wl.a1);
    } else wallSpan(wl, wl.a0, wl.a1);
  }

  // A thin panel on a wall's corridor face: position by span, height range, and standoff.
  const onWall = (wl, a0, a1, y0, y1, standoff, m, depth = 0.04) => {
    const mid = (a0 + a1) / 2;
    const o = wl.n * (standoff + depth / 2);
    const [x, z] = wl.axis === "u" ? planToLocal(mid, wl.at + o) : planToLocal(wl.at + o, mid);
    return box(a1 - a0, y1 - y0, depth, m, x, (y0 + y1) / 2, z, wl.axis === "u" ? Math.PI / 2 : 0);
  };
  for (const f of F.features) {
    const wl = walls[f.wall];
    const { a0, a1 } = f;
    const mid = (a0 + a1) / 2;
    if (f.kind === "door" || f.kind === "double") {
      onWall(wl, a0 - 0.06, a1 + 0.06, 0, 2.2, 0, mat.frame, 0.03);
      if (f.kind === "double") {
        onWall(wl, a0, mid - 0.01, 0, 2.13, 0.03, mat.door, 0.03);
        onWall(wl, mid + 0.01, a1, 0, 2.13, 0.03, mat.door, 0.03);
      } else onWall(wl, a0, a1, 0, 2.13, 0.03, mat.door, 0.03);
      onWall(wl, a0, a1, 0, 0.25, 0.06, mat.kick, 0.01);
      if (f.sign) {
        const signMat = new THREE.MeshStandardMaterial({ color: f.sign === "women" ? 0xc9a74a : 0xe6b923, metalness: 0.4, roughness: 0.4 });
        onWall(wl, mid - 0.13, mid + 0.13, 1.55, 1.81, 0.07, signMat, 0.01);
      }
      if (f.exit) onWall(wl, a0 + 0.08, a1 - 0.08, 1.0, 1.06, 0.07, mat.steel, 0.03); // panic bar
    } else if (f.kind === "elevator") {
      onWall(wl, a0 - 0.12, a1 + 0.12, 0, 2.35, 0, mat.frame, 0.05);
      onWall(wl, a0, mid - 0.005, 0, 2.15, 0.05, mat.door, 0.02);
      onWall(wl, mid + 0.005, a1, 0, 2.15, 0.05, mat.door, 0.02);
      onWall(wl, a1 + 0.3, a1 + 0.42, 1.0, 1.6, 0, mat.steel, 0.02); // call buttons
    } else if (f.kind === "glass") {
      onWall(wl, a0 - 0.08, a1 + 0.08, 0, 2.3, 0, new THREE.MeshStandardMaterial({ color: 0x3a4148, roughness: 0.5 }), 0.05);
      onWall(wl, a0, mid - 0.03, 0.05, 2.2, 0.05, mat.glass, 0.02);
      onWall(wl, mid + 0.03, a1, 0.05, 2.2, 0.05, mat.glass, 0.02);
    } else if (f.kind === "board") {
      onWall(wl, a0, a1, 1.0, 1.95, 0, mat.cork, 0.02);
    } else if (f.kind === "case") {
      onWall(wl, a0, a1, 1.15, 2.0, 0, mat.wood, 0.1);
    } else if (f.kind === "fountain") {
      onWall(wl, a0, a1, 0.7, 1.0, 0, mat.steel, 0.4);
      onWall(wl, a0 + 0.1, a0 + 0.45, 0.2, 1.7, 0, mat.steel, 0.15);
    }
  }

  for (const o of F.objects) {
    const [x, z] = planToLocal(o.u, o.v);
    box(o.w, o.h, o.d, new THREE.MeshStandardMaterial({ color: o.color, roughness: 0.6 }), x, o.h / 2, z);
  }

  const roomLabels = [];
  const doorMarkers = [];
  for (const door of DOORS.filter((d) => d.placed)) {
    const inward = door.wall === "far" ? -1 : 1;
    const [x, z] = planToLocal(door.u + inward * 0.12, door.v);
    const sprite = label(group, door.title, x, 2.35, z, "#ffffff", 0.45);
    sprite.userData.aspect = sprite.scale.x / sprite.scale.y;
    sprite.material.sizeAttenuation = false;
    roomLabels.push({ sprite, door });
    const [sx, sz] = planToLocal(CENTER_U, door.v);
    const points = [new THREE.Vector3(sx, 0.09, sz), new THREE.Vector3(x, 0.09, z)];
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color: 0xffc06b, depthTest: false }));
    const marker = new THREE.Mesh(new THREE.TorusGeometry(0.3, 0.035, 6, 24), new THREE.MeshBasicMaterial({ color: 0xffc06b }));
    marker.rotation.x = Math.PI / 2;
    marker.position.set(sx, 0.09, sz);
    group.add(line, marker);
    doorMarkers.push({ doorId: door.id, line, marker });
  }
  const origin = new THREE.Mesh(new THREE.TorusGeometry(0.45, 0.04, 6, 32), new THREE.MeshBasicMaterial({ color: 0x45d5a2 }));
  origin.rotation.x = Math.PI / 2;
  origin.position.set(0, 0.08, -CENTER_U);
  group.add(origin);
  const originLabel = label(group, "418 · JUNCTION · 0 m", 0, 3.8, -CENTER_U, "#45d5a2", 0.5);
  originLabel.userData.aspect = originLabel.scale.x / originLabel.scale.y;
  originLabel.material.sizeAttenuation = false;
  roomLabels.push({ sprite: originLabel, door: null });
  group.userData.roomLabels = roomLabels;
  group.userData.doorMarkers = doorMarkers;
  group.userData.occluders = occluders;

  return { group };
}

// Sample points along the hallways, in plan coordinates, used to fit the plan into the footprint.
function floor4Samples(F = FLOOR4) {
  const pts = [];
  const um = (F.junctionNear + F.junctionFar) / 2;
  for (let u = F.stemStart; u <= F.junctionNear; u += 1) pts.push([u, 0]);
  for (let v = F.leftEnd; v <= F.rightEnd; v += 1) pts.push([um, v]);
  return { pts, rightEnd: [um, F.rightEnd], leftEnd: [um, F.leftEnd] };
}

// Middle of the floor's overall extent (main hallway back wall to the fountain recess, stair
// end to glass doors), in plan coordinates. This is the point that gets centered in ECS.
function floor4Center(F = FLOOR4) {
  return [(F.stemStart + F.junctionFar + F.alcove.depth) / 2, (F.leftEnd + F.rightEnd) / 2];
}

// Plan (u, v) → world (x, z) for a rotation phi and translation (tx, tz).
function planToWorld(u, v, phi, tx, tz) {
  const [lx, lz] = planToLocal(u, v);
  const c = Math.cos(phi), s = Math.sin(phi);
  return [lx * c - lz * s + tx, lx * s + lz * c + tz];
}

function pointInPoly([x, z], ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, zi] = ring[i], [xj, zj] = ring[j];
    if (zi > z !== zj > z && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) inside = !inside;
  }
  return inside;
}

function distToRing([x, z], ring) {
  let best = Infinity;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [ax, az] = ring[j], [bx, bz] = ring[i];
    const dx = bx - ax, dz = bz - az, L = dx * dx + dz * dz || 1;
    const t = Math.max(0, Math.min(1, ((x - ax) * dx + (z - az) * dz) / L));
    best = Math.min(best, Math.hypot(x - ax - t * dx, z - az - t * dz));
  }
  return best;
}

// Rectangle around the whole outline with the smallest area (fallback for odd outlines).
function minAreaRect(ring) {
  let best = null;
  for (let i = 0; i < ring.length; i++) {
    const [ax, az] = ring[i], [bx, bz] = ring[(i + 1) % ring.length];
    const ang = Math.atan2(bz - az, bx - ax);
    const c = Math.cos(-ang), s = Math.sin(-ang);
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    for (const [px, pz] of ring) {
      const rx = px * c - pz * s, rz = px * s + pz * c;
      x0 = Math.min(x0, rx); x1 = Math.max(x1, rx); z0 = Math.min(z0, rz); z1 = Math.max(z1, rz);
    }
    const area = (x1 - x0) * (z1 - z0);
    if (!best || area < best.area) {
      const mx = (x0 + x1) / 2, mz = (z0 + z1) / 2;
      best = {
        area,
        center: [mx * Math.cos(ang) - mz * Math.sin(ang), mx * Math.sin(ang) + mz * Math.cos(ang)],
        angle: x1 - x0 >= z1 - z0 ? ang : ang + Math.PI / 2,
      };
    }
  }
  return best;
}

// Largest rectangle that fits inside the outline, aligned with one of the outline's wall
// directions. The outline is rasterized at 0.5 m in each candidate direction and the biggest
// all-inside block of cells is found (largest-rectangle-in-histogram, row by row).
function mainBlock(ring, cell = 0.5) {
  // Candidate directions: distinct wall angles (mod 90°), longest walls first.
  const dirs = [];
  ring
    .map((p, i) => {
      const q = ring[(i + 1) % ring.length];
      return { len: Math.hypot(q[0] - p[0], q[1] - p[1]), ang: Math.atan2(q[1] - p[1], q[0] - p[0]) };
    })
    .filter((e) => e.len > 3)
    .sort((a, b) => b.len - a.len)
    .forEach(({ ang }) => {
      const a = ((ang % (Math.PI / 2)) + Math.PI / 2) % (Math.PI / 2);
      if (dirs.length < 8 && !dirs.some((d) => Math.abs(d - a) < 0.02 || Math.abs(d - a) > Math.PI / 2 - 0.02)) dirs.push(a);
    });

  let best = null;
  for (const ang of dirs) {
    const c = Math.cos(-ang), s = Math.sin(-ang);
    const r = ring.map(([x, z]) => [x * c - z * s, x * s + z * c]);
    const xs = r.map((p) => p[0]), zs = r.map((p) => p[1]);
    const x0 = Math.min(...xs), z0 = Math.min(...zs);
    const cols = Math.ceil((Math.max(...xs) - x0) / cell), rows = Math.ceil((Math.max(...zs) - z0) / cell);
    if (cols * rows > 400000) continue; // absurdly large outline; skip this direction
    const heights = new Array(cols).fill(0);
    for (let row = 0; row < rows; row++) {
      // Scanline: which cells in this row have their center inside the outline.
      const zc = z0 + (row + 0.5) * cell;
      const hits = [];
      for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
        const [xi, zi] = r[i], [xj, zj] = r[j];
        if (zi > zc !== zj > zc) hits.push(xi + ((zc - zi) / (zj - zi)) * (xj - xi));
      }
      hits.sort((a, b) => a - b);
      const inside = new Array(cols).fill(false);
      for (let k = 0; k + 1 < hits.length; k += 2) {
        const from = Math.max(0, Math.ceil((hits[k] - x0) / cell - 0.5));
        const to = Math.min(cols - 1, Math.floor((hits[k + 1] - x0) / cell - 0.5));
        for (let col = from; col <= to; col++) inside[col] = true;
      }
      for (let col = 0; col < cols; col++) heights[col] = inside[col] ? heights[col] + 1 : 0;
      // Largest rectangle under this row's histogram.
      const stack = [];
      for (let col = 0; col <= cols; col++) {
        const h = col < cols ? heights[col] : 0;
        while (stack.length && heights[stack[stack.length - 1]] >= h) {
          const top = stack.pop();
          const height = heights[top];
          const left = stack.length ? stack[stack.length - 1] + 1 : 0;
          const width = col - left;
          const area = width * height * cell * cell;
          if (height && (!best || area > best.area)) {
            const mx = x0 + (left + width / 2) * cell, mz = z0 + (row + 1 - height / 2) * cell;
            const w = width * cell, d = height * cell;
            best = {
              area, w, d,
              center: [mx * Math.cos(ang) - mz * Math.sin(ang), mx * Math.sin(ang) + mz * Math.cos(ang)],
              angle: w >= d ? ang : ang + Math.PI / 2,
            };
          }
        }
        stack.push(col);
      }
    }
  }
  return best;
}

// Finds where the hallway plan most plausibly sits inside the ECS footprint: the cross hallway
// runs along one of the footprint's main axes, as much of the plan as possible lies inside, and
// the right branch's glass doors (an exit to outside) end at an exterior wall.
function fitFloor4(ring, F = FLOOR4) {
  if (!ring || ring.length < 3) return { turns: 0, phi0: 0, phi: 0, tx: 0, tz: 0, inside: 0 };
  // The building's main block: the largest rectangle that fits inside the outline. Wings and
  // annexes attached to it don't move its center, unlike a box drawn around the whole outline.
  const bestAxis = mainBlock(ring) ?? minAreaRect(ring);
  const phi0 = bestAxis.angle;
  const [cx, cz] = bestAxis.center;

  const { pts, rightEnd } = floor4Samples(F);
  const midU = (F.junctionNear + F.junctionFar) / 2, midV = (F.leftEnd + F.rightEnd) / 2;
  const evaluate = (turns, ox, oz) => {
    const phi = phi0 + (turns * Math.PI) / 2;
    // Offsets are relative to putting the plan's middle on the footprint centroid.
    const [mx, mz] = planToWorld(midU, midV, phi, 0, 0);
    const tx = cx - mx + ox, tz = cz - mz + oz;
    let inside = 0;
    for (const [u, v] of pts) if (pointInPoly(planToWorld(u, v, phi, tx, tz), ring)) inside++;
    const re = planToWorld(rightEnd[0], rightEnd[1], phi, tx, tz);
    const exteriorDoor = distToRing(re, ring) < 2.5 ? 3 : 0;
    const score = inside * 10 + exteriorDoor - Math.hypot(ox, oz) * 0.05;
    return { score, turns, phi, tx, tz, ox, oz, inside: inside / pts.length };
  };
  // Best placement for one orientation: coarse 2 m grid, then refine around it on a 0.5 m grid.
  const bestFor = (turns) => {
    let best = null;
    for (let ox = -30; ox <= 30; ox += 2) {
      for (let oz = -30; oz <= 30; oz += 2) {
        const r = evaluate(turns, ox, oz);
        if (!best || r.score > best.score) best = r;
      }
    }
    const coarse = best;
    for (let ox = coarse.ox - 2; ox <= coarse.ox + 2; ox += 0.5) {
      for (let oz = coarse.oz - 2; oz <= coarse.oz + 2; oz += 0.5) {
        const r = evaluate(turns, ox, oz);
        if (r.score > best.score) best = r;
      }
    }
    return best;
  };
  const perTurn = [0, 1, 2, 3].map(bestFor);
  const shapeBest = perTurn.reduce((a, b) => (b.score > a.score ? b : a));
  // The footprint can't tell which way the main hallway faces; F.halfTurns sets that.
  const chosen = perTurn[(shapeBest.turns + 2 * (F.halfTurns || 0)) % 4];
  // Placement: the search above only decides orientation. Position is exact centering: the
  // middle of the floor's overall extent goes on the center of the building's main block.
  const [cu, cv] = floor4Center(F);
  const [ux, uz] = planToWorld(cu, cv, chosen.phi, 0, 0);
  const tx = bestAxis.center[0] - ux, tz = bestAxis.center[1] - uz;
  let inside = 0;
  for (const [u, v] of pts) if (pointInPoly(planToWorld(u, v, chosen.phi, tx, tz), ring)) inside++;
  return { ...chosen, tx, tz, inside: inside / pts.length, phi0 };
}


// ---------------------------------------------------------------------------
// Robots in the 4th-floor hallways.
// The backend's 0–100 position grid is laid over the floor plan:
//   x 0 → 100  spans the cross hallway from the stair end (left) to the glass doors (right)
//   y 0 → 100  spans forward from the back of the main hallway to the cross hallway's far wall
// and every position is snapped to the nearest point a robot can occupy, so robots never sit
// in a wall. Moving between the main hallway and the cross hallway routes through the junction.
// ---------------------------------------------------------------------------
const ROBOT_RADIUS = 0.35; // keeps the 0.62 m robot body clear of the walls
const ROBOT_SPACE_KEY = "robot-space-v1";

// Walkable rectangles in plan coordinates (both convex; they overlap in the junction).
function hallwayRects(F = FLOOR4) {
  const r = ROBOT_RADIUS;
  return {
    stem: { u0: F.stemStart + r, u1: F.junctionFar - r, v0: -F.stemHalfWidth + r, v1: F.stemHalfWidth - r },
    cross: { u0: F.junctionNear + r, u1: F.junctionFar - r, v0: F.leftEnd + r, v1: F.rightEnd - r },
  };
}
const inRect = (R, u, v) => u >= R.u0 - 1e-6 && u <= R.u1 + 1e-6 && v >= R.v0 - 1e-6 && v <= R.v1 + 1e-6;

// Points already in a hallway are kept; anything else goes to the nearest point on a hallway's
// center line, so robots travel down the middle instead of hugging the walls.
function snapToHallway(u, v, F = FLOOR4) {
  const { stem, cross } = hallwayRects(F);
  if (inRect(stem, u, v) || inRect(cross, u, v)) return [u, v];
  const midU = (F.junctionNear + F.junctionFar) / 2;
  const a = [Math.min(stem.u1, Math.max(stem.u0, u)), 0];
  const b = [midU, Math.min(cross.v1, Math.max(cross.v0, v))];
  return Math.hypot(a[0] - u, a[1] - v) <= Math.hypot(b[0] - u, b[1] - v) ? a : b;
}

function robotToHallway(pos, F = FLOOR4) {
  const p = pos || { x: 50, y: 50 };
  const v = F.leftEnd + (p.x / 100) * (F.rightEnd - F.leftEnd);
  const u = F.stemStart + (p.y / 100) * (F.junctionFar - F.stemStart);
  return snapToHallway(u, v, F);
}

// Waypoints (plan coordinates) from a to b that stay inside the hallways.
function hallwayRoute([ua, va], [ub, vb], F = FLOOR4) {
  const { stem, cross } = hallwayRects(F);
  const sameRect = (inRect(stem, ua, va) && inRect(stem, ub, vb)) || (inRect(cross, ua, va) && inRect(cross, ub, vb));
  if (sameRect) return [[ub, vb]];
  const junction = [(F.junctionNear + F.junctionFar) / 2, 0]; // lies in both rectangles
  return [junction, [ub, vb]];
}


const OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"];
// Bounding box around the CSULB campus, used only to locate ECS by name.
const CSULB_BBOX = "33.7740,-118.1250,33.7900,-118.1050";
const ECS_NAME = "Engineering (and|&) Computer Science|^ECS$";
// The processed map is cached in the browser so Overpass is only queried once.
// (v2: drops layouts cached by the earlier version, which could be centered on the wrong building.)
const MAP_CACHE_KEY = `csulb-ecs-layout-v2-r${WORLD_RADIUS}`;

// Ask both Overpass servers, staggered: the backup starts if the main server hasn't answered
// within HEDGE_MS (or fails sooner). First good answer wins; the other request is cancelled.
const HEDGE_MS = 2500;
const OVERPASS_TIMEOUT_MS = 30000;

function overpass(query, onProgress = () => {}) {
  const body = "data=" + encodeURIComponent(query);
  return new Promise((resolve, reject) => {
    let settled = false, started = 0, failed = 0, lastErr;
    const ctls = [], timers = [];
    const finish = (fn, v) => {
      if (settled) return;
      settled = true;
      timers.forEach(clearTimeout);
      ctls.forEach((c) => c.abort());
      fn(v);
    };
    const start = () => {
      if (settled || started >= OVERPASS.length) return;
      const url = OVERPASS[started++];
      const host = new URL(url).host;
      const ctl = new AbortController();
      ctls.push(ctl);
      timers.push(setTimeout(() => ctl.abort(), OVERPASS_TIMEOUT_MS));
      fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body,
        signal: ctl.signal,
      })
        .then(async (res) => {
          if (!res.ok) throw new Error(`${host} returned HTTP ${res.status}`);
          // Stream the body so the UI can show how much has arrived.
          const reader = res.body.getReader();
          const chunks = [];
          let bytes = 0;
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            chunks.push(value);
            bytes += value.length;
            if (!settled) onProgress(`Downloading map data… ${Math.round(bytes / 1024)} KB`);
          }
          return JSON.parse(new TextDecoder().decode(await new Blob(chunks).arrayBuffer()));
        })
        .then((data) => finish(resolve, data))
        .catch((e) => {
          if (settled) return;
          lastErr = e.name === "AbortError" ? new Error(`${host} timed out`) : e;
          if (++failed >= OVERPASS.length) finish(reject, lastErr);
          else start(); // fail over immediately instead of waiting for the hedge timer
        });
    };
    onProgress("Contacting OpenStreetMap…");
    start();
    timers.push(setTimeout(start, HEDGE_MS));
  });
}

// ---------------------------------------------------------------------------
// OpenStreetMap import: finds ECS, downloads everything around it from the
// Overpass API, projects it to local meters and clips it to WORLD_RADIUS.
// ---------------------------------------------------------------------------

function makeProjection(lat0, lon0) {
  const kx = 111320 * Math.cos((lat0 * Math.PI) / 180);
  const kz = 110540;
  return (lat, lon) => [round((lon - lon0) * kx), round(-(lat - lat0) * kz)];
}
const round = (v) => Math.round(v * 100) / 100;

function ringArea(r) {
  let a = 0;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) a += (r[j][0] + r[i][0]) * (r[j][1] - r[i][1]);
  return a / 2;
}

function ringCentroid(r) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const f = r[j][0] * r[i][1] - r[i][0] * r[j][1];
    a += f;
    cx += (r[j][0] + r[i][0]) * f;
    cy += (r[j][1] + r[i][1]) * f;
  }
  if (Math.abs(a) < 1e-12) return r[0];
  return [cx / (3 * a), cy / (3 * a)];
}

function openRing(pts) {
  const r = pts.slice();
  if (r.length > 1 && r[0][0] === r.at(-1)[0] && r[0][1] === r.at(-1)[1]) r.pop();
  return r;
}

// The perimeter as a convex polygon, so Sutherland–Hodgman clipping is exact enough (< 1 cm error at 256 sides).
const CIRCLE = Array.from({ length: 256 }, (_, i) => {
  const t = (i / 256) * Math.PI * 2;
  return [Math.cos(t) * WORLD_RADIUS, Math.sin(t) * WORLD_RADIUS];
});

function clipPolygon(ring) {
  let out = ring;
  for (let i = 0; i < CIRCLE.length && out.length; i++) {
    const a = CIRCLE[i], b = CIRCLE[(i + 1) % CIRCLE.length];
    const inside = (p) => (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= 0;
    const cross = (p, q) => {
      const x1 = p[0], y1 = p[1], x2 = q[0], y2 = q[1];
      const x3 = a[0], y3 = a[1], x4 = b[0], y4 = b[1];
      const d = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4);
      const t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / d;
      return [x1 + t * (x2 - x1), y1 + t * (y2 - y1)];
    };
    const input = out;
    out = [];
    for (let j = 0; j < input.length; j++) {
      const cur = input[j], prev = input[(j + input.length - 1) % input.length];
      if (inside(cur)) {
        if (!inside(prev)) out.push(cross(prev, cur));
        out.push(cur);
      } else if (inside(prev)) out.push(cross(prev, cur));
    }
  }
  return out.map(([x, z]) => [round(x), round(z)]);
}

// Clip a polyline to the circle, splitting it wherever it leaves the perimeter.
function clipLine(pts) {
  const pieces = [];
  let cur = [];
  const inC = (p) => p[0] * p[0] + p[1] * p[1] <= WORLD_RADIUS * WORLD_RADIUS;
  const hits = (p, q) => {
    const dx = q[0] - p[0], dz = q[1] - p[1];
    const A = dx * dx + dz * dz, B = 2 * (p[0] * dx + p[1] * dz), C = p[0] * p[0] + p[1] * p[1] - WORLD_RADIUS * WORLD_RADIUS;
    const disc = B * B - 4 * A * C;
    if (A === 0 || disc < 0) return [];
    const s = Math.sqrt(disc);
    return [(-B - s) / (2 * A), (-B + s) / (2 * A)].filter((t) => t > 0 && t < 1).sort((a, b) => a - b);
  };
  const at = (p, q, t) => [round(p[0] + t * (q[0] - p[0])), round(p[1] + t * (q[1] - p[1]))];
  for (let i = 0; i < pts.length; i++) {
    const p = pts[i];
    if (i === 0) {
      if (inC(p)) cur.push(p);
      continue;
    }
    const prev = pts[i - 1];
    for (const t of hits(prev, p)) {
      const x = at(prev, p, t);
      if (cur.length) {
        cur.push(x);
        pieces.push(cur);
        cur = [];
      } else cur.push(x);
    }
    if (inC(p)) cur.push(p);
    else if (cur.length) {
      pieces.push(cur);
      cur = [];
    }
  }
  if (cur.length) pieces.push(cur);
  return pieces.filter((s) => s.length >= 2);
}

// Join a multipolygon relation's member ways into closed rings.
function stitchRings(segments) {
  const rings = [];
  const pool = segments.map((s) => s.slice());
  const key = (p) => `${p[0]},${p[1]}`;
  while (pool.length) {
    let ring = pool.shift();
    let grew = true;
    while (key(ring[0]) !== key(ring.at(-1)) && grew) {
      grew = false;
      for (let i = 0; i < pool.length; i++) {
        const s = pool[i];
        if (key(s[0]) === key(ring.at(-1))) ring = ring.concat(s.slice(1));
        else if (key(s.at(-1)) === key(ring.at(-1))) ring = ring.concat(s.slice().reverse().slice(1));
        else if (key(s.at(-1)) === key(ring[0])) ring = s.concat(ring.slice(1));
        else if (key(s[0]) === key(ring[0])) ring = s.slice().reverse().concat(ring.slice(1));
        else continue;
        pool.splice(i, 1);
        grew = true;
        break;
      }
    }
    if (ring.length >= 4) rings.push(ring);
  }
  return rings;
}

// Tag interpretation

function parseMeters(v) {
  if (v == null) return null;
  const n = parseFloat(String(v).replace(",", "."));
  if (!Number.isFinite(n)) return null;
  return /ft|'/.test(String(v)) ? n * 0.3048 : n;
}

function buildingHeight(tags) {
  const h = parseMeters(tags.height) ?? parseMeters(tags["building:height"]);
  if (h) return { height: round(h), heightSource: "osm:height" };
  const levels = parseFloat(tags["building:levels"]);
  if (Number.isFinite(levels) && levels > 0) return { height: round(levels * 3.8 + 1.2), heightSource: "osm:levels" };
  const guess = { garage: 3.5, shed: 3, roof: 4, parking: 10 }[tags.building] ?? 12;
  return { height: guess, heightSource: "default" };
}

const PATH_WIDTH = {
  footway: 3, path: 2.5, pedestrian: 6, steps: 2.5, cycleway: 2.5, corridor: 2.5,
  service: 5, living_street: 6, residential: 8, unclassified: 8, tertiary: 10, secondary: 12, primary: 14,
};
function pathKind(tags) {
  const h = tags.highway;
  if (["footway", "path", "pedestrian", "steps", "corridor", "cycleway"].includes(h)) return h === "cycleway" ? "cycleway" : h === "steps" ? "steps" : "footway";
  return h === "service" ? "service" : "road";
}

function areaKind(tags) {
  if (tags.amenity === "parking" || tags.landuse === "garages") return "parking";
  if (tags.natural === "water" || tags.water || tags.leisure === "swimming_pool") return "water";
  if (tags.highway === "pedestrian" || tags.place === "square" || tags.area === "yes" && tags.highway) return "plaza";
  if (["grass", "meadow", "recreation_ground", "village_green", "flowerbed"].includes(tags.landuse)) return "grass";
  if (["park", "garden", "pitch", "playground"].includes(tags.leisure)) return "grass";
  if (["grassland", "scrub", "wood", "heath"].includes(tags.natural)) return "grass";
  if (tags.landuse) return null; // e.g. landuse=education covers the whole campus; skip it
  return null;
}

// Outline + centroid (lat/lon) of a building element from an "out geom" response.
function buildingOutline(el) {
  const pts = el.geometry ?? el.members?.filter((m) => m.role !== "inner" && m.geometry).flatMap((m) => m.geometry);
  if (!pts?.length) return null;
  const lat0 = pts[0].lat, lon0 = pts[0].lon;
  const proj = makeProjection(lat0, lon0);
  // Relations are stitched into real rings; flattening their member ways would give a bogus centroid.
  const rings = el.geometry
    ? [openRing(el.geometry.map((g) => proj(g.lat, g.lon)))]
    : stitchRings(el.members.filter((m) => m.role !== "inner" && m.geometry).map((m) => m.geometry.map((g) => proj(g.lat, g.lon)))).map(openRing);
  const ring = rings.sort((a, b) => Math.abs(ringArea(b)) - Math.abs(ringArea(a)))[0];
  if (!ring || ring.length < 3) return null;
  const [cx, cz] = ringCentroid(ring);
  return {
    ring,
    proj,
    lat: lat0 - cz / 110540,
    lon: lon0 + cx / (111320 * Math.cos((lat0 * Math.PI) / 180)),
  };
}


const describe = (el) => el.tags?.name || el.tags?.ref || `unnamed building ${el.type}/${el.id}`;

// Only what the renderer actually draws. Notably no blanket landuse query, which would pull
// in the outline of the entire CSULB campus (landuse=education) for nothing.
const featureQuery = (at) => `
  way${at}["building"];
  relation${at}["building"];
  way${at}["highway"];
  way${at}["landuse"~"^(grass|meadow|recreation_ground|village_green|flowerbed|garages)$"];
  way${at}["leisure"~"^(park|garden|pitch|playground|swimming_pool)$"];
  relation${at}["leisure"~"^(park|garden|pitch)$"];
  way${at}["natural"~"^(water|grassland|scrub|wood|heath)$"];
  way${at}["amenity"="parking"];
  node${at}["natural"="tree"];
  node${at}["entrance"];`;
// Short declared limits get scheduled sooner on the public Overpass servers.
const QUERY_HEAD = "[out:json][timeout:25][maxsize:33554432];";
// Overpass measures "around" to way segments, so long paths crossing the perimeter are
// still caught; a few meters of slack covers rounding.
const FETCH_RADIUS = WORLD_RADIUS + 10;
const ECS_RE = new RegExp(ECS_NAME, "i");

function rankEcs(el) {
  const n = (el.tags?.name || "").toLowerCase();
  let sc = 0;
  if (el.tags?.ref === "ECS") sc += 4;
  if (/^engineering (and|&) computer sciences?( \(ecs\))?$/.test(n)) sc += 3;
  else if (/engineering (and|&) computer science/.test(n)) sc += 2;
  if (/parking|structure|annex|lot/.test(n)) sc -= 5;
  return sc;
}

// Automatic mode: one request that finds ECS by name *and* returns everything around it.
async function downloadAroundEcs(onProgress) {
  const raw = await overpass(
    `${QUERY_HEAD}
(
  way["building"]["name"~"${ECS_NAME}",i](${CSULB_BBOX});
  relation["building"]["name"~"${ECS_NAME}",i](${CSULB_BBOX});
  way["building"]["ref"="ECS"](${CSULB_BBOX});
  relation["building"]["ref"="ECS"](${CSULB_BBOX});
)->.ecs;
(${featureQuery(`(around.ecs:${FETCH_RADIUS})`)}
);
out geom tags qt;`,
    onProgress
  );
  // Rank candidates instead of taking whichever OSM lists first.
  const candidates = raw.elements
    .filter((el) => el.tags?.building && (ECS_RE.test(el.tags.name || "") || el.tags.ref === "ECS"))
    .map((el) => ({ el, o: buildingOutline(el), sc: rankEcs(el) }))
    .filter((c) => c.o)
    .sort((a, b) => b.sc - a.sc);
  if (!candidates.length) {
    throw new Error("ECS was not found by name in OpenStreetMap");
  }
  const { el, o } = candidates[0];
  const center = {
    lat: o.lat,
    lon: o.lon,
    name: describe(el),
    osmId: `${el.type}/${el.id}`,
    method: "name search",
    others: candidates.slice(1).map((c) => `${describe(c.el)} (${c.el.type}/${c.el.id})`),
  };
  return { center, raw };
}


function build(center, raw) {
  const proj = makeProjection(center.lat, center.lon);
  const toPts = (geom) => geom.filter(Boolean).map((g) => proj(g.lat, g.lon));
  const layout = {
    source: "OpenStreetMap contributors (ODbL) via Overpass API",
    generated: new Date().toISOString(),
    center: { lat: round6(center.lat), lon: round6(center.lon) },
    anchor: { name: center.name, osmId: center.osmId, method: center.method, others: center.others || [] },
    radius: WORLD_RADIUS,
    units: "meters; +x east, +z south; ECS centroid at origin",
    buildings: [],
    areas: [],
    paths: [],
    trees: [],
    entrances: [],
  };

  const polygonsOf = (el) => {
    if (el.type === "way" && el.geometry) return [{ outer: openRing(toPts(el.geometry)), holes: [] }];
    if (el.type === "relation" && el.members) {
      const outer = stitchRings(el.members.filter((m) => m.role !== "inner" && m.geometry).map((m) => toPts(m.geometry)));
      const inner = stitchRings(el.members.filter((m) => m.role === "inner" && m.geometry).map((m) => toPts(m.geometry)));
      return outer.map((o) => ({ outer: openRing(o), holes: inner.map(openRing) }));
    }
    return [];
  };
  const clipPoly = ({ outer, holes }) => {
    const o = clipPolygon(outer);
    if (o.length < 3 || Math.abs(ringArea(o)) < 0.5) return null;
    return { outer: o, holes: holes.map(clipPolygon).filter((h) => h.length >= 3) };
  };

  const seen = new Set();
  for (const el of raw.elements) {
    const id = `${el.type}/${el.id}`;
    if (seen.has(id)) continue;
    seen.add(id);
    const tags = el.tags || {};

    if (el.type === "node") {
      const p = proj(el.lat, el.lon);
      if (Math.hypot(p[0], p[1]) > WORLD_RADIUS) continue;
      if (tags.natural === "tree") layout.trees.push(p);
      else if (tags.entrance) layout.entrances.push({ id, x: p[0], z: p[1], name: tags.name ?? tags.ref ?? null, kind: tags.entrance });
      continue;
    }

    if (tags.building && tags.building !== "no") {
      const isAnchor = id === center.osmId;
      for (const poly of polygonsOf(el)) {
        const c = clipPoly(poly);
        if (!c) continue;
        layout.buildings.push({
          id, name: tags.name ?? null, ref: tags.ref ?? null, kind: tags.building,
          ...buildingHeight(tags),
          minHeight: parseMeters(tags.min_height) ?? 0,
          anchor: isAnchor, ...c,
        });
      }
      continue;
    }

    const ak = areaKind(tags);
    const g = el.geometry;
    const closed = el.type === "relation" || (g && g.length > 3 && g[0].lat === g.at(-1).lat && g[0].lon === g.at(-1).lon);
    if (ak && closed) {
      for (const poly of polygonsOf(el)) {
        const c = clipPoly(poly);
        if (c) layout.areas.push({ id, kind: ak, name: tags.name ?? null, ...c });
      }
      continue;
    }

    if (tags.highway && el.geometry) {
      const width = parseMeters(tags.width) ?? PATH_WIDTH[tags.highway] ?? 4;
      for (const pts of clipLine(toPts(el.geometry))) {
        layout.paths.push({ id, kind: pathKind(tags), width: round(width), name: tags.name ?? null, points: pts });
      }
    }
  }

  // Draw large areas first so smaller ones (lawns inside plazas) sit on top.
  layout.areas.sort((a, b) => Math.abs(ringArea(b.outer)) - Math.abs(ringArea(a.outer)));
  return layout;
}
const round6 = (v) => Math.round(v * 1e6) / 1e6;

// One download shared by every caller: React dev mode mounts the scene twice, and without
// this both mounts would hit Overpass at once and compete for the same rate-limit slots.
let layoutRequest = null;

function fetchCampusLayout(onProgress = () => {}) {
  try {
    const cached = JSON.parse(localStorage.getItem(MAP_CACHE_KEY) || "null");
    if (cached?.buildings) return Promise.resolve({ layout: cached, cached: true, ms: 0 });
  } catch {
    // Corrupt or unavailable storage: fall through to a fresh download.
  }
  const listeners = layoutRequest?.listeners ?? new Set();
  listeners.add(onProgress);
  if (layoutRequest) return layoutRequest.promise;
  const report = (msg) => listeners.forEach((fn) => fn(msg));
  const t0 = performance.now();
  const promise = (async () => {
    const { center, raw } = await downloadAroundEcs(report);
    report("Building 3D campus…");
    const layout = build(center, raw);
    try {
      localStorage.setItem(MAP_CACHE_KEY, JSON.stringify(layout));
    } catch {
      // Storage full or blocked; the map just reloads from Overpass next time.
    }
    return { layout, cached: false, ms: Math.round(performance.now() - t0) };
  })().finally(() => {
    layoutRequest = null;
  });
  layoutRequest = { promise, listeners };
  return promise;
}


// Scene units are meters, centered on the ECS building (+x east, -z north).
// Robots still report positions on a 0-100 grid; that grid now spans the full
// 200 m square around the 100 m perimeter, so each robot unit = 2 m and (50, 50) is ECS.
// y increases northward, matching the old hallway's orientation.
const ROBOT_SPAN = WORLD_RADIUS * 2;
function toScene(pos) {
  const p = pos || { x: 50, y: 50 };
  return { x: (p.x / 100 - 0.5) * ROBOT_SPAN, z: (0.5 - p.y / 100) * ROBOT_SPAN };
}

const HOME = new THREE.Vector3(0, 125, 140); // overview camera: whole 100 m radius in frame
const BASE_FOV = 55;
// Zoom limits per camera mode. Overview zooms by camera distance, Follow by how far
// behind the robot the camera trails, Robot POV by narrowing the field of view.
const ZOOM = {
  step: 1.35, // one button press / key press
  overview: { min: 6, max: 360 }, // meters from the orbit target
  follow: { min: 0.25, max: 5 }, // multiplier on the default follow offset
  pov: { minFov: 12, maxFov: 90 },
};

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

function buildRobotMesh(scene, name) {
  const g = new THREE.Group();
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(0.62, 0.3, 0.46),
    new THREE.MeshStandardMaterial({ color: COLORS.alive, emissive: COLORS.alive, emissiveIntensity: 0.18, metalness: 0.25 })
  );
  body.position.y = 0.3;
  g.add(body);
  const top = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.12, 0.25), new THREE.MeshStandardMaterial({ color: 0xf0f6fb, metalness: 0.25 }));
  top.position.y = 0.49;
  g.add(top);
  [-0.2, 0.2].forEach((x) => {
    const w = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, 0.08, 12), new THREE.MeshStandardMaterial({ color: 0x0b1520 }));
    w.rotation.z = Math.PI / 2;
    w.position.set(x, 0.13, 0.2);
    g.add(w);
  });
  const ant = new THREE.Mesh(
    new THREE.CylinderGeometry(0.018, 0.018, 0.22, 8),
    new THREE.MeshStandardMaterial({ color: 0xffbd67, emissive: 0xffbd67, emissiveIntensity: 0.7 })
  );
  ant.position.y = 0.67;
  g.add(ant);
  const ring = new THREE.Mesh(new THREE.TorusGeometry(0.5, 0.025, 8, 24), new THREE.MeshBasicMaterial({ color: COLORS.alive, transparent: true, opacity: 0.8 }));
  ring.rotation.x = Math.PI / 2;
  ring.position.y = 0.04;
  g.add(ring);
  scene.add(g);
  const nameLabel = label(scene, name, 0, 1.4, 0, "#ffffff", 0.5);
  nameLabel.userData.aspect = nameLabel.scale.x / nameLabel.scale.y;
  nameLabel.material.sizeAttenuation = false;
  return { group: g, body, ring, nameLabel, trail: [], line: null };
}

function useScene(viewRef, robotsRef, uiRef, onPick, onWorld, zoomRef, onZoom, onFloor) {
  useEffect(() => {
    const view = viewRef.current;
    let disposed = false;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x07131f);
    // Max camera distance is 360 m; keep the full 100 m campus visible from there.
    scene.fog = new THREE.Fog(0x07131f, 600, 1100);
    const cam = new THREE.PerspectiveCamera(55, 1, 0.1, 2000);
    cam.position.copy(HOME);
    const ren = new THREE.WebGLRenderer({ antialias: true });
    ren.localClippingEnabled = true; // used to cut away ECS above the 4th floor in the 4F view
    ren.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    view.appendChild(ren.domElement);
    scene.add(new THREE.HemisphereLight(0xbcd8f0, 0x1a2a37, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 2.4);
    sun.position.set(60, 120, 40); // afternoon sun from the southwest-ish, high in the sky
    scene.add(sun);

    // Overview camera can be orbited / panned / zoomed around the campus.
    const controls = new OrbitControls(cam, ren.domElement);
    controls.target.set(0, 0, 0);
    controls.enableDamping = true;
    controls.maxPolarAngle = Math.PI * 0.47; // never dip below the ground
    controls.minDistance = ZOOM.overview.min;
    controls.maxDistance = ZOOM.overview.max;
    controls.zoomSpeed = 1.2;
    controls.screenSpacePanning = false;

    // ---- ECS 4th floor ----
    const floor = { group: null, slab: null, anchorMeshes: [], fit: null, view: null };
    let floorOn = false;
    const cutPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), FLOOR4.elevation + 1.5);
    function setupFloor4(info) {
      if (floor.group) {
        scene.add(robotLayer); // preserve robots before disposing the provisional floor
        scene.remove(floor.group);
        disposeScene(floor.group);
        if (floor.slab) { scene.remove(floor.slab); disposeScene(floor.slab); floor.slab = null; }
        robotSpace = null; // reparent robots into the replacement floor on the next frame
      }
      const ring = info.anchor?.outer ?? null;
      floor.fit = fitFloor4(ring);
      const built = buildFloor4();
      floor.group = built.group;
      scene.add(floor.group);
      floor.anchorMeshes = info.anchorMeshes || [];
      // The ECS footprint as a slab at floor level, shown in the 4F cutaway view.
      if (ring) {
        const shape = new THREE.Shape(ring.map(([x, z]) => new THREE.Vector2(x, -z)));
        const g = new THREE.ShapeGeometry(shape);
        g.rotateX(-Math.PI / 2);
        floor.slab = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: 0x33475a, roughness: 0.9, side: THREE.DoubleSide }));
        floor.slab.position.y = FLOOR4.elevation - 0.08;
        floor.slab.visible = false;
        scene.add(floor.slab);
      }
      placeFloor4();
      setFloorMode(floorOn); // cutaway only in the 4F view
    }
    // Places the floor where the automatic fit put it (centered in ECS's main block).
    function placeFloor4() {
      const { phi, tx, tz } = floor.fit;
      const midU = (FLOOR4.junctionNear + FLOOR4.junctionFar) / 2;
      floor.group.rotation.y = -phi;
      floor.group.position.set(tx, FLOOR4.elevation, tz);
      // Camera framing for the 4F view: look "forward" along the main hallway from behind it.
      const [cx, cz] = planToWorld(midU - 4, 0, phi, tx, tz);
      const [bx, bz] = planToWorld(midU - 34, 0, phi, tx, tz);
      floor.view = {
        target: new THREE.Vector3(cx, FLOOR4.elevation, cz),
        pos: new THREE.Vector3(bx, FLOOR4.elevation + 42, bz),
      };
      onFloor.current?.({ inside: floor.fit.inside, hasFootprint: !!floor.slab });
    }
    function setFloorMode(on) {
      for (const m of floor.anchorMeshes) {
        m.material.clippingPlanes = on ? [cutPlane] : [];
        if (m.isMesh) m.material.opacity = on ? 0.3 : 0.55;
      }
      if (floor.slab) floor.slab.visible = on;
    }

    // Ground and perimeter render immediately; the campus streams in from OpenStreetMap.
    groundAndPerimeter(scene);
    const placeholder = new THREE.Group();
    buildPlaceholderWorld(placeholder);
    scene.add(placeholder);
    setupFloor4({ anchor: null, anchorMeshes: [] }); // usable immediately, even without internet
    onWorld.current?.({ state: "loading", message: "Loading CSULB map data around ECS…" });
    fetchCampusLayout((message) => {
      if (!disposed) onWorld.current?.({ state: "loading", message });
    })
      .then(({ layout, cached, ms }) => {
        if (disposed) return;
        scene.remove(placeholder);
        disposeScene(placeholder);
        const campus = new THREE.Group();
        const info = buildCampusWorld(campus, layout);
        scene.add(campus);
        setupFloor4(info);
        onWorld.current?.({
          state: "loaded",
          cached,
          ms,
          center: layout.center,
          anchor: layout.anchor,
          buildings: new Set(layout.buildings.map((b) => b.id)).size,
          paths: layout.paths.length,
          trees: layout.trees.length,
          guessedHeights: layout.buildings.filter((b) => b.heightSource === "default").length,
        });
      })
      .catch((e) => {
        if (disposed) return;
        scene.remove(placeholder);
        disposeScene(placeholder);
        onWorld.current?.({ state: "error", message: e.message || String(e) });
        // The measured floor is already available at the map center.
      });

    const meshes = new Map(); // robot name -> mesh record

    // Robots and their trails live in one layer. In hallway mode the layer is parented to the
    // 4th-floor group, so robot coordinates are floor-local (x = v, z = -u, y = 0 is the floor).
    const robotLayer = new THREE.Group();
    scene.add(robotLayer);
    let robotSpace = null;

    function targetFor(r, space) {
      if (space === "hallway") {
        const [u, v] = r.state?.pose?.space === "floor4"
          ? [r.state.pose.u, r.state.pose.v]
          : robotToHallway(r.state?.position);
        const [x, z] = planToLocal(u, v);
        return { x, z, u, v };
      }
      return toScene(r.state?.position);
    }

    function setRobotSpace(space) {
      robotSpace = space;
      (space === "hallway" ? floor.group : scene).add(robotLayer); // reparents
      for (const m of meshes.values()) m.reset = true; // jump to the new space, restart trails
    }

    function syncMeshes() {
      const robots = robotsRef.current;
      const want = uiRef.current.robotSpace === "hallway" && floor.group ? "hallway" : "campus";
      if (want !== robotSpace) setRobotSpace(want);
      const names = new Set(robots.map((r) => r.name));
      for (const [name, m] of meshes) {
        if (!names.has(name)) {
          robotLayer.remove(m.group);
          robotLayer.remove(m.nameLabel);
          disposeScene(m.group);
          disposeScene(m.nameLabel);
          if (m.line) robotLayer.remove(m.line);
          meshes.delete(name);
        }
      }
      for (const r of robots) {
        let m = meshes.get(r.name);
        const target = targetFor(r, robotSpace);
        if (!m) {
          m = buildRobotMesh(robotLayer, r.name);
          m.group.userData.name = r.name;
          m.reset = true;
          meshes.set(r.name, m);
        }
        m.hex = robotHex(r);
        m.authoritative = robotSpace === "hallway" && r.state?.pose?.space === "floor4";
        if (m.authoritative) {
          m.heading = r.state.pose.heading;
          m.target = target;
          m.route = [];
          if (m.reset) m.group.position.set(target.x, 0, target.z);
          m.reset = false;
          const previous = m.trail[m.trail.length - 1];
          if (!previous || Math.hypot(previous[0] - target.x, previous[1] - target.z) > 0.03) {
            m.trail.push([target.x, target.z]);
            if (m.trail.length > TRAIL_MAX) m.trail.shift();
            m.trailDirty = true;
          }
          continue;
        }
        if (m.reset) {
          m.reset = false;
          m.group.position.set(target.x, 0, target.z);
          m.route = [];
          m.trail = [[target.x, target.z]];
          m.trailDirty = true;
          m.target = target;
          continue;
        }
        if (m.target && Math.hypot(m.target.x - target.x, m.target.z - target.z) < 0.05) continue;
        // New destination: plan a route from where the robot is now.
        const pos = m.group.position;
        if (robotSpace === "hallway") {
          const here = snapToHallway(-pos.z, pos.x);
          m.route = hallwayRoute(here, [target.u, target.v]).map(([u, v]) => {
            const [x, z] = planToLocal(u, v);
            return { x, z };
          });
        } else m.route = [{ x: target.x, z: target.z }];
        m.target = target;
        for (const w of m.route) m.trail.push([w.x, w.z]);
        while (m.trail.length > TRAIL_MAX) m.trail.shift();
        m.trailDirty = true;
      }
    }

    // Moves a robot along its route; faster when far behind, so it keeps up with updates.
    function stepRobot(m, dt) {
      if (m.authoritative) {
        const alpha = 1 - Math.exp(-12 * dt);
        m.group.position.x = THREE.MathUtils.lerp(m.group.position.x, m.target.x, alpha);
        m.group.position.z = THREE.MathUtils.lerp(m.group.position.z, m.target.z, alpha);
        return;
      }
      if (!m.route?.length) return;
      let remaining = 0;
      let px = m.group.position.x, pz = m.group.position.z;
      for (const w of m.route) {
        remaining += Math.hypot(w.x - px, w.z - pz);
        px = w.x;
        pz = w.z;
      }
      const speed = Math.max(robotSpace === "hallway" ? 0.8 : 2, remaining * 2.5);
      let budget = speed * dt;
      while (budget > 0 && m.route.length) {
        const w = m.route[0];
        const dx = w.x - m.group.position.x, dz = w.z - m.group.position.z;
        const d = Math.hypot(dx, dz);
        if (d > 1e-3) m.heading = Math.atan2(-dz, dx); // robot's long (+x) axis faces travel
        if (d <= budget) {
          m.group.position.x = w.x;
          m.group.position.z = w.z;
          m.route.shift();
          budget -= d;
        } else {
          m.group.position.x += (dx / d) * budget;
          m.group.position.z += (dz / d) * budget;
          budget = 0;
        }
      }
    }

    function drawTrail(name, m) {
      const { trails, selected } = uiRef.current;
      if (m.line && (m.trailDirty || m.lineSel !== (selected === name) || !trails)) {
        robotLayer.remove(m.line);
        disposeScene(m.line);
        m.line = null;
      }
      if (!trails || m.line || m.trail.length < 2) return;
      const pts = m.trail.map((p) => new THREE.Vector3(p[0], 0.08, p[1]));
      const sel = selected === name;
      m.line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(pts),
        new THREE.LineBasicMaterial({ color: sel ? 0x6aaaff : 0x45d5a2, transparent: true, opacity: sel ? 1 : 0.45 })
      );
      m.lineSel = sel;
      m.trailDirty = false;
      robotLayer.add(m.line);
    }

    function size() {
      ren.setSize(view.clientWidth, view.clientHeight);
      cam.aspect = view.clientWidth / view.clientHeight;
      cam.updateProjectionMatrix();
    }
    window.addEventListener("resize", size);
    size();

    const ray = new THREE.Raycaster();
    const mouse = new THREE.Vector2();
    // Picking happens on release so dragging the camera doesn't open the robot modal.
    let down = null;
    let followDrag = null;
    function pointerDown(e) {
      down = [e.clientX, e.clientY];
      if (uiRef.current.view === "follow" && e.button === 0) {
        followDrag = [e.clientX, e.clientY];
        view.setPointerCapture(e.pointerId);
      }
    }
    function rotateFollow(e) {
      if (!followDrag || uiRef.current.view !== "follow") return;
      followYaw -= (e.clientX - followDrag[0]) * 0.006;
      followElevation = THREE.MathUtils.clamp(followElevation + (e.clientY - followDrag[1]) * 0.005, 0.12, 1.45);
      followDrag = [e.clientX, e.clientY];
    }
    function pick(e) {
      followDrag = null;
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
      down = null;
      const rect = view.getBoundingClientRect();
      mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      ray.setFromCamera(mouse, cam);
      const groups = [...meshes.values()].map((m) => m.group);
      const hit = ray.intersectObjects(groups, true)[0];
      if (!hit) return;
      let g = hit.object;
      while (g.parent && !groups.includes(g)) g = g.parent;
      if (g.userData.name) onPick.current(g.userData.name);
    }
    view.addEventListener("pointerdown", pointerDown);
    view.addEventListener("pointerup", pick);
    view.addEventListener("pointermove", rotateFollow);
    const cancelDrag = () => { followDrag = null; down = null; };
    view.addEventListener("pointercancel", cancelDrag);

    // In Follow / POV the orbit controls are off, so the wheel feeds the same zoom
    // queue as the buttons. In the overview, OrbitControls handles the wheel itself.
    let tracking = false;
    function wheel(e) {
      if (!tracking) return;
      e.preventDefault();
      zoomRef.current.pending += THREE.MathUtils.clamp(e.deltaY, -100, 100) * 0.003;
    }
    view.addEventListener("wheel", wheel, { passive: false });

    let raf;
    let lastMode = "overview";
    let returning = false; // easing the camera to a preset (HOME or the 4F view)
    let flyTo = { pos: HOME, target: new THREE.Vector3() };
    let followMul = 1; // Follow-mode zoom
    let followYaw = 0;
    let followElevation = 0.8;
    let followedRobot = null;
    let povFov = BASE_FOV; // POV-mode zoom
    let lastLevel = null;
    let previousFrame = performance.now();
    const pW = new THREE.Vector3();
    const qW = new THREE.Quaternion();
    const fwd = new THREE.Vector3();
    const tmp = new THREE.Vector3();
    const origin = new THREE.Vector3();
    const homeDist = HOME.length();
    const labelPoint = new THREE.Vector3();
    const occlusionRay = new THREE.Raycaster();
    const overlaps = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;

    // Applies a zoom factor (< 1 zooms in) to whichever camera mode is active.
    function applyZoom(f, mode) {
      if (mode === "follow") {
        followMul = THREE.MathUtils.clamp(followMul * f, ZOOM.follow.min, ZOOM.follow.max);
      } else if (mode === "pov") {
        povFov = THREE.MathUtils.clamp(povFov * f, ZOOM.pov.minFov, ZOOM.pov.maxFov);
      } else {
        returning = false; // a manual zoom takes over from the ease to a preset
        const d = THREE.MathUtils.clamp(cam.position.distanceTo(controls.target) * f, ZOOM.overview.min, ZOOM.overview.max);
        cam.position.sub(controls.target).setLength(d).add(controls.target);
      }
    }

    function zoomLevel(mode) {
      if (mode === "follow") return 1 / followMul;
      if (mode === "pov") return BASE_FOV / povFov;
      const base = mode === "floor4" && floor.view ? floor.view.pos.distanceTo(floor.view.target) : homeDist;
      return base / cam.position.distanceTo(controls.target);
    }
    function loop() {
      raf = requestAnimationFrame(loop);
      syncMeshes();
      const { view: mode, selected } = uiRef.current;
      placeholder.visible = mode === "overview";
      const now = performance.now();
      const dt = Math.min((now - previousFrame) / 1000, 0.1);
      previousFrame = now;
      // Robots are ~0.6 m long; enlarge them in wide views so they read at that scale.
      // In the hallways they stay life-size except in the far-away campus overview.
      const robotScale = robotSpace === "hallway" ? (mode === "overview" ? 2.5 : 1) : mode === "overview" || mode === "floor4" ? 4 : 1;
      const nameRects = [];
      for (const [name, m] of meshes) {
        stepRobot(m, dt);
        const s = THREE.MathUtils.lerp(m.group.scale.x, robotScale, 0.1);
        m.group.scale.set(s, s, s);
        if (m.heading !== undefined) {
          // Turn smoothly toward the direction of travel (shortest way round).
          const diff = Math.atan2(Math.sin(m.heading - m.group.rotation.y), Math.cos(m.heading - m.group.rotation.y));
          m.group.rotation.y += diff * Math.min(1, dt * 8);
        }
        m.body.material.color.setHex(m.hex);
        m.body.material.emissive.setHex(m.hex);
        m.ring.material.color.setHex(m.hex);
        m.nameLabel.position.copy(m.group.position).y = 1.2 * s;
        const labelHeight = 2 * Math.tan(THREE.MathUtils.degToRad(cam.fov / 2)) * (selected === name ? 17 : 14) / view.clientHeight;
        m.nameLabel.scale.set(labelHeight * m.nameLabel.userData.aspect, labelHeight, 1);
        m.nameLabel.material.color.setHex(selected === name ? 0xffc06b : 0xffffff);
        m.nameLabel.visible = uiRef.current.robotLabels && !(mode === "pov" && selected === name);
        if (m.nameLabel.visible) {
          m.nameLabel.getWorldPosition(labelPoint).project(cam);
          const rect = { x: (labelPoint.x + 1) * view.clientWidth / 2, y: (1 - labelPoint.y) * view.clientHeight / 2, w: 17 * m.nameLabel.userData.aspect + 4, h: 21 };
          let shifts = 0;
          while (nameRects.some((other) => overlaps(rect, other)) && shifts < 6) { rect.y -= 21; shifts++; }
          m.nameLabel.visible = shifts < 6 && Math.abs(labelPoint.z) <= 1;
          if (m.nameLabel.visible) {
            labelPoint.y += shifts * 42 / view.clientHeight;
            labelPoint.unproject(cam);
            m.nameLabel.position.copy(robotLayer.worldToLocal(labelPoint));
            nameRects.push(rect);
          }
        }
        drawTrail(name, m);
      }
      const sel = meshes.get(selected);
      // World-space position and facing of the selected robot (its layer may be the floor group).
      const p = sel ? sel.group.getWorldPosition(pW) : origin;
      if (sel) fwd.set(1, 0, 0).applyQuaternion(sel.group.getWorldQuaternion(qW)).setY(0).normalize();
      if (mode === "follow" && sel && followedRobot !== selected) {
        followYaw = Math.atan2(-fwd.x, -fwd.z);
        followElevation = 0.8;
        followedRobot = selected;
      } else if (mode !== "follow") followedRobot = null;
      if (floor.group) {
        const labelRects = [];
        const labels = [...floor.group.userData.roomLabels].sort((a, b) =>
          Number(b.door?.id === uiRef.current.doorId) - Number(a.door?.id === uiRef.current.doorId));
        for (const { sprite, door } of labels) {
          if (door) sprite.position.y = mode === "follow" || mode === "pov" ? 1.8 : 2.35;
          const close = !door || !sel || Math.abs(door.v - sel.group.position.x) < 12 || door.id === uiRef.current.doorId;
          sprite.visible = uiRef.current.roomLabels && (mode === "floor4" || ((mode === "follow" || mode === "pov") && close));
          const height = 2 * Math.tan(THREE.MathUtils.degToRad(cam.fov / 2)) * (door?.id === uiRef.current.doorId ? 16 : 13) / view.clientHeight;
          sprite.scale.set(height * sprite.userData.aspect, height, 1);
          sprite.material.color.setHex(door?.id === uiRef.current.doorId ? 0xffc06b : 0xffffff);
          if (sprite.visible) {
            sprite.getWorldPosition(labelPoint).project(cam);
            const rect = { x: (labelPoint.x + 1) * view.clientWidth / 2, y: (1 - labelPoint.y) * view.clientHeight / 2, w: 16 * sprite.userData.aspect + 6, h: 20 };
            sprite.visible = Math.abs(labelPoint.z) <= 1 && !labelRects.some((other) => overlaps(rect, other));
            if (sprite.visible) labelRects.push(rect);
          }
        }
        for (const mark of floor.group.userData.doorMarkers) {
          mark.line.visible = mark.marker.visible = mark.doorId === uiRef.current.doorId && mode !== "overview";
        }
      }
      const indoors = robotSpace === "hallway";
      tracking = Boolean((mode === "follow" || mode === "pov") && sel);
      const floorView = mode === "floor4" && floor.view;
      const camMode = tracking ? mode : floorView ? "floor4" : "overview";
      if (camMode !== lastMode && !tracking) returning = true;
      flyTo = floorView ? floor.view : { pos: HOME, target: origin };
      // Cut the building away in the 4F view, and when following a robot inside the hallways.
      const wantCut = camMode === "floor4" || (indoors && tracking);
      if (floorOn !== wantCut) {
        floorOn = wantCut;
        setFloorMode(floorOn);
      }
      lastMode = camMode;

      // Zoom requests are queued (in log space) and eased in over a few frames.
      const z = zoomRef.current;
      if (z.reset) {
        z.reset = false;
        z.pending = 0;
        if (camMode === "follow") {
          followMul = 1;
          followYaw = Math.atan2(-fwd.x, -fwd.z);
          followElevation = 0.8;
        }
        else if (camMode === "pov") povFov = BASE_FOV;
        else returning = true;
      }
      if (z.pending) {
        const step = Math.abs(z.pending) < 0.002 ? z.pending : z.pending * 0.25;
        z.pending -= step;
        applyZoom(Math.exp(step), camMode);
      }
      const fov = camMode === "pov" ? povFov : BASE_FOV;
      if (Math.abs(cam.fov - fov) > 0.01) {
        cam.fov = THREE.MathUtils.lerp(cam.fov, fov, 0.2);
        cam.updateProjectionMatrix();
      }

      controls.enabled = !tracking && !returning;
      if (mode === "follow" && sel) {
        const radius = (indoors ? 5.3 : 9) * followMul;
        const horizontal = Math.cos(followElevation) * radius;
        tmp.set(p.x + Math.sin(followYaw) * horizontal, p.y + Math.sin(followElevation) * radius + 0.4, p.z + Math.cos(followYaw) * horizontal);
        cam.position.lerp(tmp, 1 - Math.exp(-12 * dt));
        cam.lookAt(p.x, p.y + 0.4, p.z);
      } else if (mode === "pov" && sel) {
        // Just behind the robot at its own eye height, looking the way it faces.
        const back = indoors ? 0.6 : 1.3, eye = indoors ? 0.95 : 2.1;
        cam.position.lerp(tmp.copy(p).addScaledVector(fwd, -back).setY(p.y + eye), 0.13);
        tmp.copy(p).addScaledVector(fwd, 4).setY(p.y + (indoors ? 0.85 : 1));
        cam.lookAt(tmp);
      } else if (returning) {
        cam.position.lerp(flyTo.pos, 0.06);
        controls.target.lerp(flyTo.target, 0.06);
        cam.lookAt(controls.target);
        if (cam.position.distanceTo(flyTo.pos) < 0.5) returning = false;
      } else {
        controls.update();
      }
      // A full follow orbit can look across a hallway wall. Fade only the surfaces
      // between camera and robot so the target remains visible from every azimuth.
      if (floor.group) {
        const blocked = new Set();
        if (tracking && indoors && sel) {
          scene.updateMatrixWorld(true);
          cam.updateMatrixWorld();
          tmp.copy(p).y += 0.35;
          const distance = tmp.distanceTo(cam.position);
          occlusionRay.set(cam.position, tmp.sub(cam.position).normalize());
          occlusionRay.far = Math.max(0, distance - 0.2);
          for (const hit of occlusionRay.intersectObjects(floor.group.userData.occluders, false)) blocked.add(hit.object);
        }
        for (const mesh of floor.group.userData.occluders) {
          const fade = blocked.has(mesh);
          if (mesh.userData.faded === fade) continue;
          mesh.userData.faded = fade;
          mesh.material.opacity = fade ? 0.12 : mesh.userData.originalOpacity;
          mesh.material.transparent = fade || mesh.userData.originalTransparent;
          mesh.material.depthWrite = !fade;
          mesh.material.needsUpdate = true;
        }
      }
      const level = Math.round(zoomLevel(camMode) * 10) / 10;
      if (level !== lastLevel) {
        lastLevel = level;
        onZoom.current?.(level);
      }
      ren.render(scene, cam);
    }
    loop();

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", size);
      view.removeEventListener("pointerdown", pointerDown);
      view.removeEventListener("pointerup", pick);
      view.removeEventListener("pointermove", rotateFollow);
      view.removeEventListener("pointercancel", cancelDrag);
      view.removeEventListener("wheel", wheel);
      controls.dispose();
      disposeScene(scene);
      ren.dispose();
      view.removeChild(ren.domElement);
    };
  }, [viewRef, robotsRef, uiRef, onPick, onWorld, zoomRef, onZoom, onFloor]);
}

export default function Page() {
  const [robots, setRobots] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [view, setView] = useState("overview");
  // Where robots are drawn: inside the ECS 4th-floor hallways (default) or on the campus map.
  const [robotSpace, setRobotSpaceState] = useState("hallway");
  const [trails, setTrails] = useState(true);
  const [robotLabels, setRobotLabels] = useState(true);
  const [roomLabels, setRoomLabels] = useState(true);
  const [doorId, setDoorId] = useState("416-a");
  const [navigationPending, setNavigationPending] = useState(false);
  const [dash, setDash] = useState(true);
  const [events, setEvents] = useState(["Dashboard connected to fleet registry"]);
  const [toastText, setToastText] = useState("");
  const [goal, setGoal] = useState("");
  const [task, setTask] = useState(null);
  const [taskMessage, setTaskMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [world, setWorld] = useState(null);
  const [zoomLevel, setZoomLevel] = useState(1);

  const viewRef = useRef(null);
  const robotsRef = useRef([]);
  const uiRef = useRef({ view, trails, selected });
  const onPick = useRef(null);
  const onWorld = useRef(setWorld);
  const zoomRef = useRef({ pending: 0, reset: false });
  const [floorInfo, setFloorInfo] = useState(null);
  const onFloor = useRef(setFloorInfo);
  const onZoom = useRef(setZoomLevel);
  const seenMsgs = useRef(new Set());
  const toastTimer = useRef(null);

  robotsRef.current = robots;
  uiRef.current = { view, trails, selected, robotSpace, robotLabels, roomLabels, doorId };
  onPick.current = (name) => {
    setSelected(name);
    setModalOpen(true);
  };

  useScene(viewRef, robotsRef, uiRef, onPick, onWorld, zoomRef, onZoom, onFloor);

  // Load the saved robot placement once (browser only).
  useEffect(() => {
    try {
      // Clean up settings from features that no longer exist (manual floor alignment, manual map center).
      ["ecs-floor4-align-v1", "ecs-floor4-align-v2", "ecs-floor4-align-v3", "ecs-floor4-align-v4", "csulb-ecs-center-v1"].forEach((k) =>
        localStorage.removeItem(k)
      );
      const saved = localStorage.getItem(ROBOT_SPACE_KEY);
      if (saved === "hallway" || saved === "campus") setRobotSpaceState(saved);
    } catch {
      // Storage unavailable: keep the default.
    }
  }, []);
  function setRobotSpace(space) {
    setRobotSpaceState(space);
    try {
      localStorage.setItem(ROBOT_SPACE_KEY, space);
    } catch {
      // Not persisted; applies for this session.
    }
    toast(space === "hallway" ? "Robots moved into the ECS 4th floor hallways" : "Robots moved to the campus map");
  }

  function zoomIn() {
    zoomRef.current.pending -= Math.log(ZOOM.step);
  }
  function zoomOut() {
    zoomRef.current.pending += Math.log(ZOOM.step);
  }
  function zoomReset() {
    zoomRef.current.reset = true;
  }

  // Keyboard: + / = zoom in, - zoom out, 0 resets. Ignored while typing in the task console.
  useEffect(() => {
    function onKey(e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.target.closest?.("input, textarea, select, [contenteditable]")) return;
      if (e.key === "+" || e.key === "=") zoomIn();
      else if (e.key === "-" || e.key === "_") zoomOut();
      else if (e.key === "0") zoomReset();
      else return;
      e.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  function log(text) {
    setEvents((ev) => [`${new Date().toLocaleTimeString()} ${text}`, ...ev].slice(0, 60));
  }

  function toast(text) {
    setToastText(text);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToastText(""), 1400);
  }

  async function refresh() {
    try {
      const res = await fetch("/api/fleet", { cache: "no-store" });
      const data = await res.json();
      if (data.error) return setError(data.error);
      setError(null);
      setRobots(data.robots);
      // Surface robot-to-robot messages in the event stream, once each.
      for (const r of data.robots) {
        for (const m of r.state?.inbox || []) {
          const key = `${r.name}|${m.ts}|${m.from}`;
          if (seenMsgs.current.has(key)) continue;
          seenMsgs.current.add(key);
          const body = m.body?.event || m.body?.msg || JSON.stringify(m.body);
          log(`${m.from} → ${r.name}: ${body}`);
        }
      }
    } catch (e) {
      setError(String(e));
    }
    try {
      const res = await fetch("/api/orchestrator/tasks", { cache: "no-store" });
      if (res.ok) setTasks((await res.json()).tasks || []);
    } catch {
      // Orchestrator is optional; the fleet still renders without it.
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!selected && robots.length) setSelected(robots[0].name);
  }, [robots, selected]);

  useEffect(() => {
    if (!task?.task_id || FINAL.includes(task.status)) return;
    const id = setInterval(async () => {
      try {
        const res = await fetch(`/api/orchestrator/tasks/${task.task_id}`, { cache: "no-store" });
        if (res.ok) setTask(await res.json());
      } catch {
        // The task stays visible while the orchestrator is temporarily unavailable.
      }
    }, 1000);
    return () => clearInterval(id);
  }, [task?.task_id, task?.status]);

  async function act(label, url, body) {
    try {
      await post(url, body);
      log(label);
      toast(label);
    } catch (e) {
      log(`${label} failed: ${e.message}`);
      toast(`Failed: ${e.message}`);
    }
    refresh();
  }

  async function navigateDoor(target, cancel = false) {
    if (!selected || navigationPending) return;
    setNavigationPending(true);
    try {
      await post(`/api/robots/${selected}/navigate`, cancel ? { cancel: true } : { door_id: target });
      log(`${selected}: ${cancel ? "route cancelled" : target === "junction" ? "returning to junction" : `navigating to ${target}`}`);
      if (!cancel) {
        setRobotSpace("hallway");
        setView("follow");
      }
      await refresh();
    } catch (error) {
      toast(error.message);
      log(`Navigation failed: ${error.message}`);
    } finally {
      setNavigationPending(false);
    }
  }

  async function submitGoal(e) {
    e.preventDefault();
    if (!goal.trim() || submitting) return;
    setSubmitting(true);
    setTaskMessage("");
    try {
      const data = await post("/api/orchestrator/chat", { message: goal.trim() });
      setTaskMessage(data.message || "Gemini processed the request.");
      if (data.executed) {
        setTask(data.executed);
        log(`Task ${data.executed.task_id} → ${data.executed.robot_id}`);
      }
      setGoal("");
      refresh();
    } catch (err) {
      setTaskMessage(`Error: ${err.message}`);
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelTask(id) {
    try {
      const data = await post(`/api/orchestrator/tasks/${id}/cancel`, { reason: "cancelled from dashboard" });
      if (task?.task_id === id) setTask(data);
      log(`Task ${id} cancelled`);
    } catch (e) {
      toast(`Cancel failed: ${e.message}`);
    }
    refresh();
  }

  // Used by the RETRY button shown when the map download fails: clears any cached map and
  // reloads the page so the download runs again.
  function reloadMap() {
    try {
      localStorage.removeItem(MAP_CACHE_KEY);
    } catch {
      // Storage unavailable: nothing cached to clear.
    }
    window.location.reload();
  }

  function changeView(v) {
    setView(v);
    toast(`Camera ${v === "floor4" ? "ECS 4TH FLOOR" : v === "pov" ? "ROBOT POV" : v.toUpperCase()}`);
  }

  const sel = robots.find((r) => r.name === selected);
  const door = DOORS.find((d) => d.id === doorId);
  const phase = sel?.state?.navigation?.phase || "idle";
  const routeActive = !["idle", "at_junction", "door_touched", "cancelled"].includes(phase);
  const canNavigate = sel && robotStatus(sel) === "alive" && sel.state?.floor_version === "measured-v1" && !routeActive && !navigationPending;
  const online = robots.filter((r) => robotStatus(r) === "alive").length;
  const activeTasks = tasks.filter((t) => !FINAL.includes(t.status));
  const doneTasks = tasks.filter((t) => t.status === "completed").length;
  const missionTask = task || activeTasks[0];
  const missionText = missionTask
    ? `${missionTask.status.toUpperCase()} · ${missionTask.robot_id} · ${missionTask.destination?.name || missionTask.payload?.destination?.name || missionTask.type}`
    : sel
    ? `READY · ${sel.name} selected`
    : "WAITING · No robots registered";

  return (
    <div className="app">
      <div className="view" ref={viewRef} />
      {robots.length === 0 && (
        <div className="empty">
          {error ? `Registry error: ${error}` : "No robots registered yet."}
          <br />
          Start robots with join.py; they appear here automatically.
        </div>
      )}

      <div className="top">
        <div>
          <div className="brand">LASTMILE // FLEET CONTROL CENTER</div>
          <div className="sub">
            CSULB · ECS · {WORLD_RADIUS} M RADIUS · {robots.length} ROBOT CONTAINER{robots.length === 1 ? "" : "S"}
            {robotSpace === "hallway" ? " IN ECS 4F HALLWAYS" : " ON CAMPUS MAP"}
          </div>
        </div>
        <div className="nav">
          {["overview", "floor4", "follow", "pov"].map((v) => (
            <button key={v} className={`btn ${view === v ? "active" : ""}`} onClick={() => changeView(v)}>
              {v === "pov" ? "ROBOT POV" : v === "floor4" ? "ECS 4F" : v.toUpperCase()}
            </button>
          ))}
          <button className="btn" onClick={() => setTrails(!trails)}>PATH TRAILS: {trails ? "ON" : "OFF"}</button>
          <button className="btn" onClick={() => setRobotLabels(!robotLabels)}>ROBOT LABELS: {robotLabels ? "ON" : "OFF"}</button>
          <button className="btn" onClick={() => setRoomLabels(!roomLabels)}>ROOM LABELS: {roomLabels ? "ON" : "OFF"}</button>
          {view === "follow" && <button className="btn" onClick={zoomReset}>RESET BEHIND ROBOT</button>}
          <button className="btn" onClick={() => setDash(!dash)}>DASHBOARD: {dash ? "ON" : "OFF"}</button>
          <div className="zoom" role="group" aria-label="Camera zoom">
            <button className="btn" onClick={zoomOut} title="Zoom out (−)" aria-label="Zoom out">−</button>
            <button className="btn zoom-level" onClick={zoomReset} title="Reset zoom (0)">{zoomLevel.toFixed(1)}×</button>
            <button className="btn" onClick={zoomIn} title="Zoom in (+)" aria-label="Zoom in">+</button>
          </div>
        </div>
      </div>

      <div className="mission">
        <div className="label">ACTIVE SCENARIO</div>
        <b>{missionText}</b>
        <div className="progress">
          <div className="bar" style={{ width: `${Math.round((missionTask?.progress || 0) * 100)}%` }} />
        </div>
        <form onSubmit={submitGoal} style={{ marginTop: 12 }}>
          <div className="label" style={{ marginBottom: 6 }}>LLM TASK CONSOLE</div>
          <textarea
            className="textarea"
            rows={2}
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="Deliver package P123 to the north entrance"
          />
          <div className="sub" style={{ marginBottom: 6 }}>north/east/west/south entrance · warehouse · main door</div>
          <button className="btn" disabled={submitting || !goal.trim()}>{submitting ? "THINKING…" : "SEND TASK"}</button>
          {missionTask && !FINAL.includes(missionTask.status) && (
            <button type="button" className="btn danger" style={{ marginLeft: 6 }} onClick={() => cancelTask(missionTask.task_id)}>
              CANCEL
            </button>
          )}
        </form>
        {taskMessage && <div className="msg muted">{taskMessage}</div>}
        {task?.replacement_task_id && (
          <div className="msg orange">reassigned to {task.replacement_robot_id} · {task.replacement_task_id}</div>
        )}
        {task?.error && <div className="msg red">{task.error}</div>}
      </div>

      {dash && (
        <aside className="side">
          <div className="card">
            <div className="card-title">
              <span>Fleet overview ({robots.length})</span>
              <span className={error ? "red" : "green"}>● {error ? "REGISTRY ERROR" : "REGISTRY ONLINE"}</span>
            </div>
            {robots.length === 0 && <div className="row muted">No robots registered</div>}
            {robots.map((r) => {
              const s = robotStatus(r);
              const bat = Math.round(r.state?.battery ?? 0);
              return (
                <div key={r.name} className={`robot-card ${selected === r.name ? "selected" : ""}`} onClick={() => onPick.current(r.name)}>
                  <div className="robot-head">
                    <b>{r.name} · {r.remote ? "remote" : "local"}</b>
                    <span className={statusClass(s)}>{s.toUpperCase()}</span>
                  </div>
                  <div className="row"><span className="muted">Mode</span><span>{r.state?.mode || "unknown"}</span></div>
                  <div className="row"><span className="muted">Battery</span><span>{r.state ? `${bat}%` : "—"} · {r.hostPort}</span></div>
                  <div className="battery"><i className={bat <= 30 ? "low" : ""} style={{ width: `${bat}%` }} /></div>
                </div>
              );
            })}
          </div>

          <div className="card">
            <div className="card-title">
              <span>ECS 4th floor</span>
              <button className={`btn ${view === "floor4" ? "active" : ""}`} onClick={() => changeView(view === "floor4" ? "overview" : "floor4")}>
                {view === "floor4" ? "EXIT 4F VIEW" : "VIEW 4F"}
              </button>
            </div>
            <div className="row">
              <span className="muted">Robots move</span>
              <span className="seg">
                <button className={`btn ${robotSpace === "hallway" ? "active" : ""}`} onClick={() => setRobotSpace("hallway")}>IN HALLWAYS</button>
                <button className={`btn ${robotSpace === "campus" ? "active" : ""}`} onClick={() => setRobotSpace("campus")}>ON CAMPUS</button>
              </span>
            </div>
            <div className="msg muted">
              Origin: junction near 418. 1 step = 0.5 m · 1 tile = 0.25 m. Door distances are provisionally measured along the hallway centerline; the final approach is additional.
            </div>
            {floorInfo && (
              <>
                {floorInfo.hasFootprint ? (
                  <div className="row"><span className="muted">Auto-fit inside footprint</span><span>{Math.round(floorInfo.inside * 100)}% of hallways</span></div>
                ) : (
                  <div className="row"><span className="muted">Footprint</span><span className="orange">none loaded; placed at map center</span></div>
                )}
              </>
            )}
          </div>

          <div className="card navigation-card">
            <div className="card-title"><span>Measured room navigation</span><span className="green">{sel?.name || "SELECT ROBOT"}</span></div>
            <label className="label" htmlFor="navigation-robot">ROBOT</label>
            <select id="navigation-robot" className="select door-select" value={selected || ""} onChange={(e) => setSelected(e.target.value)}>
              {!robots.length && <option value="">Start a robot with join.py</option>}
              {robots.map((r) => <option key={r.name} value={r.name}>{r.name} · {robotStatus(r)}</option>)}
            </select>
            <label className="label" htmlFor="door-target">DESTINATION DOOR</label>
            <select id="door-target" className="select door-select" value={doorId} onChange={(e) => setDoorId(e.target.value)}>
              {DOORS.map((d) => <option key={d.id} value={d.id}>{d.title} · {d.distance.toFixed(2)} m{!d.placed ? " · UNPLACED" : ""}</option>)}
            </select>
            <div className="row"><span>From junction</span><b>{door.steps} steps · {door.distance.toFixed(2)} m · {door.distance / FLOOR_DATA.tileMeters} tiles</b></div>
            <div className="row"><span>Branch / wall side</span><b>{door.placed ? `${door.branch} / ${door.side}` : "Needs confirmation"}</b></div>
            <div className="msg muted">Left/right wall sides are viewed walking away from the junction.</div>
            {door.note && <div className="msg orange">{door.note}</div>}
            <div className="navigation-actions">
              <button className="btn" disabled={!canNavigate || !door.placed} onClick={() => navigateDoor(doorId)}>GO TO DOOR</button>
              <button className="btn" disabled={!canNavigate} onClick={() => navigateDoor("junction")}>RETURN TO JUNCTION</button>
              <button className="btn danger" disabled={!sel?.state || !routeActive || navigationPending || sel.power === "off"} onClick={() => navigateDoor(null, true)}>CANCEL ROUTE</button>
            </div>
            {sel?.state && !sel.state.floor_version && <div className="msg orange">Rebuild and recreate this robot with join.py to enable measured navigation.</div>}
            <div className="row"><span>Phase</span><b className={phase === "door_touched" ? "green" : "blue"}>{phase.replaceAll("_", " ")}</b></div>
            {sel?.state?.navigation?.door_id && <div className="row"><span>Robot destination</span><b>{sel.state.navigation.door_id}</b></div>}
            {sel?.state?.pose && <div className="row"><span>Floor pose (m)</span><span>u {sel.state.pose.u.toFixed(2)} · v {sel.state.pose.v.toFixed(2)}</span></div>}
            <div className="msg muted">Travel → stop → turn → slow approach → door touched. This simulates closed-door contact; it does not open doors or enter rooms.</div>
            {view === "follow" && <div className="msg green">Drag the map to rotate 360° around the robot. Scroll to zoom.</div>}
            <details className="door-directory">
              <summary>All measured entrances ({DOORS.length})</summary>
              {DOORS.map((d) => <button key={d.id} className={`door-entry ${doorId === d.id ? "chosen" : ""}`} onClick={() => { setDoorId(d.id); setView("floor4"); }}>
                <span>{d.title}{!d.placed ? " · unplaced" : ""}</span><span>{d.distance.toFixed(2)} m</span>
              </button>)}
            </details>
          </div>

          <div className="card">
            <div className="card-title">
              <span>Map center</span>
              <span className={world?.state === "loaded" ? "green" : world?.state === "error" ? "red" : "muted"}>
                {(world?.state || "…").toUpperCase()}
              </span>
            </div>
            {world?.state === "loaded" && (
              <>
                <div className="row"><span className="muted">Centered on</span><b>{world.anchor?.name || "—"}</b></div>
                <div className="row"><span className="muted">Coordinates</span><span>{world.center.lat.toFixed(6)}, {world.center.lon.toFixed(6)}</span></div>
                <div className="row">
                  <span className="muted">Check it</span>
                  <a className="blue" target="_blank" rel="noreferrer"
                    href={`https://www.openstreetmap.org/?mlat=${world.center.lat}&mlon=${world.center.lon}#map=18/${world.center.lat}/${world.center.lon}`}>
                    open this spot on OpenStreetMap ↗
                  </a>
                </div>
                {world.anchor?.others?.length > 0 && (
                  <div className="row"><span className="muted">Other matches</span><span>{world.anchor.others.join("; ")}</span></div>
                )}
              </>
            )}
          </div>

          <div className="card">
            <div className="card-title"><span>Task queue</span><span className="muted">LIVE</span></div>
            {tasks.length === 0 && <div className="row muted">No tasks yet</div>}
            {tasks.slice(-8).reverse().map((t) => (
              <div key={t.task_id} className="row">
                <span>{t.payload?.package_id || t.type} → {t.robot_id}</span>
                <span className={t.status === "completed" ? "green" : FINAL.includes(t.status) ? "red" : "orange"}>
                  {t.status}{!FINAL.includes(t.status) && t.progress ? ` ${Math.round(t.progress * 100)}%` : ""}
                </span>
              </div>
            ))}
          </div>

          <div className="card">
            <div className="card-title"><span>Analytics</span></div>
            <div className="row"><span>Robots alive</span><b>{online}/{robots.length}</b></div>
            <div className="row"><span>Active tasks</span><b>{activeTasks.length}</b></div>
            <div className="row"><span>Completed tasks</span><b>{doneTasks}/{tasks.length}</b></div>
            <div className="row">
              <span>Avg battery</span>
              <b>{robots.length ? Math.round(robots.reduce((a, r) => a + (r.state?.battery ?? 0), 0) / robots.length) : 0}%</b>
            </div>
          </div>

          <div className="card">
            <div className="card-title"><span>Registry / P2P network</span></div>
            <div className="row"><span>Registry</span><b className={error ? "red" : "green"}>{error ? "ERROR" : "ONLINE"}</b></div>
            <div className="row"><span>Local containers</span><b>{robots.filter((r) => !r.remote).length}</b></div>
            <div className="row"><span>Remote robots</span><b>{robots.filter((r) => r.remote).length}</b></div>
            <div className="row"><span>Refresh</span><b>1 sec</b></div>
          </div>

          <div className="card">
            <div className="card-title"><span>Event stream</span></div>
            <div className="events">{events.map((x, i) => <div key={i}>• {x}</div>)}</div>
          </div>
        </aside>
      )}

      {world?.state === "loading" && <div className="world-note info">{world.message}</div>}
      {world?.state === "error" && (
        <div className="world-note">
          Campus map failed to load: {world.message}. The measured floor remains available at the map center.
          <br />
          <button className="btn" style={{ marginTop: 6 }} onClick={reloadMap}>RETRY</button>
        </div>
      )}

      <div className="legend">
        <span><i className="dot" style={{ background: "#45d5a2" }} />alive</span>
        <span><i className="dot" style={{ background: "#6aaaff" }} />on task</span>
        <span><i className="dot" style={{ background: "#ffc06b" }} />no response</span>
        <span><i className="dot" style={{ background: "#ff7684" }} />dead</span>
        <span><i className="dot" style={{ background: "#5b636f" }} />powered off</span>
        {world?.state === "loaded" && (
          <div className="legend-map">
            {world.buildings} buildings · {world.paths} path segments · {world.trees} trees
            {world.guessedHeights ? ` · ${world.guessedHeights} default heights` : ""}
            {world.cached ? " · from cache" : ` · downloaded in ${(world.ms / 1000).toFixed(1)} s`} · map data © OpenStreetMap contributors
          </div>
        )}
      </div>

      {modalOpen && sel && (
        <div className="modal-bg" onClick={(e) => e.target === e.currentTarget && setModalOpen(false)}>
          <div className="modal">
            <div className="modal-head">
              <span>{sel.name} · {sel.remote ? "REMOTE" : "LOCAL CONTAINER"}</span>
              <button className="close" onClick={() => setModalOpen(false)}>×</button>
            </div>
            <div className="modal-grid">
              <div><small>STATUS</small><strong className={statusClass(robotStatus(sel))}>{robotStatus(sel).toUpperCase()}</strong></div>
              <div><small>BATTERY</small><strong>{sel.state ? `${Math.round(sel.state.battery)}%` : "—"}</strong></div>
              <div><small>MODE</small><strong>{sel.state?.mode || "unknown"}</strong></div>
              <div><small>ACCEPTS TASKS</small><strong>{sel.state?.accepting_tasks ? "yes" : "no"}</strong></div>
              <div><small>POSITION</small><strong>{sel.state?.position ? `${sel.state.position.x.toFixed(1)}, ${sel.state.position.y.toFixed(1)}` : "—"}</strong></div>
              <div><small>ADDRESS</small><strong>{sel.hostPort}</strong></div>
            </div>

            <div className="label">POWER {sel.remote && <span className="muted">(control from its own machine)</span>}</div>
            <div className="modal-actions">
              <button className={`btn ${sel.power === "on" ? "active" : ""}`} disabled={sel.remote}
                onClick={() => act(`${sel.name} powered on`, `/api/robots/${sel.name}/power`, { on: true })}>START</button>
              <button className={`btn warn ${sel.power === "off" ? "active" : ""}`} disabled={sel.remote}
                onClick={() => act(`${sel.name} powered off`, `/api/robots/${sel.name}/power`, { on: false })}>STOP</button>
              <button className="btn" disabled={!sel.state}
                onClick={() => { setModalOpen(false); changeView("pov"); }}>ROBOT POV</button>
            </div>

            <div className="label" style={{ marginTop: 12 }}>STATUS / BATTERY</div>
            <div className="modal-actions">
              {[["alive", "RECOVER", ""], ["no_response", "NO RESPONSE", "warn"], ["dead", "FAIL ROBOT", "danger"]].map(([s, text, cls]) => (
                <button key={s} className={`btn ${cls} ${sel.reachable && sel.state?.status === s ? "active" : ""}`} disabled={sel.power === "off"}
                  onClick={() => act(`${sel.name} set to ${s}`, `/api/robots/${sel.name}/control`, { status: s })}>{text}</button>
              ))}
              <button className="btn" disabled={sel.power === "off"}
                onClick={() => act(`${sel.name} charged`, `/api/robots/${sel.name}/control`, { battery: 100 })}>CHARGE</button>
              <button className="btn warn" disabled={sel.power === "off"}
                onClick={() => act(`${sel.name} drained`, `/api/robots/${sel.name}/control`, { battery: 10 })}>DRAIN</button>
              <button className="btn" onClick={() => { setModalOpen(false); changeView("follow"); }}>FOLLOW</button>
            </div>
          </div>
        </div>
      )}

      <div className={`toast ${toastText ? "show" : ""}`}>{toastText}</div>
    </div>
  );
}
