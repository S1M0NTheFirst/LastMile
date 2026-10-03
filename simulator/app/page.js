"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

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

// Robot positions are 0-100 on both axes; the hallway is 28 x 5 scene units.
function toScene(pos) {
  const p = pos || { x: 50, y: 50 };
  return { x: (p.x / 100) * 28 - 14, z: 2.5 - (p.y / 100) * 5 };
}

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

function buildRobotMesh(scene) {
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
  return { group: g, body, ring, trail: [], line: null };
}

function useScene(viewRef, robotsRef, uiRef, onPick) {
  useEffect(() => {
    const view = viewRef.current;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x07131f);
    const cam = new THREE.PerspectiveCamera(65, 1, 0.1, 100);
    cam.position.set(0, 13, 18);
    const ren = new THREE.WebGLRenderer({ antialias: true });
    ren.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    view.appendChild(ren.domElement);
    scene.add(new THREE.AmbientLight(0xa8c8e6, 1.7));
    const sun = new THREE.DirectionalLight(0xffffff, 2.8);
    sun.position.set(4, 12, 8);
    scene.add(sun);

    const cube = (x, y, z, w, h, d, c) => {
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), new THREE.MeshStandardMaterial({ color: c, roughness: 0.45 }));
      m.position.set(x, y, z);
      scene.add(m);
      return m;
    };
    cube(0, -0.2, 0, 30, 0.35, 6, 0x263b4d);
    const grid = new THREE.GridHelper(30, 30, 0x4d6b80, 0x355166);
    grid.scale.z = 0.23;
    grid.position.y = 0.02;
    scene.add(grid);
    cube(0, 1.6, -3, 30, 3, 0.18, 0x42566a);
    cube(0, 1.6, 3, 30, 3, 0.18, 0x42566a);
    for (let x = -10; x <= 10; x += 4) cube(x, 0.85, -2.88, 1.45, 1.7, 0.1, 0x32495e);
    cube(11, 1.25, -2.8, 3.2, 2.5, 0.12, 0x55718a);
    cube(11, 0.9, -2.65, 1.1, 1.8, 0.06, 0x66aaff);

    const meshes = new Map(); // robot name -> mesh record

    function syncMeshes() {
      const robots = robotsRef.current;
      const names = new Set(robots.map((r) => r.name));
      for (const [name, m] of meshes) {
        if (!names.has(name)) {
          scene.remove(m.group);
          if (m.line) scene.remove(m.line);
          meshes.delete(name);
        }
      }
      for (const r of robots) {
        let m = meshes.get(r.name);
        const target = toScene(r.state?.position);
        if (!m) {
          m = buildRobotMesh(scene);
          m.group.position.set(target.x, 0, target.z);
          m.group.userData.name = r.name;
          meshes.set(r.name, m);
        }
        m.target = target;
        m.hex = robotHex(r);
        const last = m.trail[m.trail.length - 1];
        if (!last || Math.hypot(last[0] - target.x, last[1] - target.z) > 0.05) {
          m.trail.push([target.x, target.z]);
          if (m.trail.length > TRAIL_MAX) m.trail.shift();
          m.trailDirty = true;
        }
      }
    }

    function drawTrail(name, m) {
      const { trails, selected } = uiRef.current;
      if (m.line && (m.trailDirty || m.lineSel !== (selected === name) || !trails)) {
        scene.remove(m.line);
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
      scene.add(m.line);
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
    function pick(e) {
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
    view.addEventListener("pointerdown", pick);

    let raf;
    const tmp = new THREE.Vector3();
    function loop() {
      raf = requestAnimationFrame(loop);
      syncMeshes();
      for (const [name, m] of meshes) {
        tmp.set(m.target.x, 0, m.target.z);
        m.group.position.lerp(tmp, 0.08);
        m.group.rotation.y += 0.008;
        m.body.material.color.setHex(m.hex);
        m.body.material.emissive.setHex(m.hex);
        m.ring.material.color.setHex(m.hex);
        drawTrail(name, m);
      }
      const { view: mode, selected } = uiRef.current;
      const sel = meshes.get(selected);
      const p = sel ? sel.group.position : new THREE.Vector3();
      if (mode === "follow" && sel) {
        cam.position.lerp(tmp.set(p.x + 5, 5.2, p.z + 6), 0.1);
        cam.lookAt(p.x, 0, p.z);
      } else if (mode === "pov" && sel) {
        cam.position.lerp(tmp.set(p.x - 1.3, 2.1, p.z + 0.6), 0.13);
        cam.lookAt(p.x + 3.3, 1, p.z);
      } else {
        cam.position.lerp(tmp.set(0, 13, 18), 0.06);
        cam.lookAt(0, 0, 0);
      }
      ren.render(scene, cam);
    }
    loop();

    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", size);
      view.removeEventListener("pointerdown", pick);
      ren.dispose();
      view.removeChild(ren.domElement);
    };
  }, [viewRef, robotsRef, uiRef, onPick]);
}

export default function Page() {
  const [robots, setRobots] = useState([]);
  const [tasks, setTasks] = useState([]);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [view, setView] = useState("overview");
  const [trails, setTrails] = useState(true);
  const [dash, setDash] = useState(true);
  const [events, setEvents] = useState(["Dashboard connected to fleet registry"]);
  const [toastText, setToastText] = useState("");
  const [goal, setGoal] = useState("");
  const [task, setTask] = useState(null);
  const [taskMessage, setTaskMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const viewRef = useRef(null);
  const robotsRef = useRef([]);
  const uiRef = useRef({ view, trails, selected });
  const onPick = useRef(null);
  const seenMsgs = useRef(new Set());
  const toastTimer = useRef(null);

  robotsRef.current = robots;
  uiRef.current = { view, trails, selected };
  onPick.current = (name) => {
    setSelected(name);
    setModalOpen(true);
  };

  useScene(viewRef, robotsRef, uiRef, onPick);

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
    const id = setInterval(refresh, 2000);
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

  function changeView(v) {
    setView(v);
    toast(`Camera ${v.toUpperCase()}`);
  }

  const sel = robots.find((r) => r.name === selected);
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
          <div className="sub">ECS HALLWAY · CLASSROOM 405 · {robots.length} ROBOT CONTAINER{robots.length === 1 ? "" : "S"}</div>
        </div>
        <div className="nav">
          {["overview", "follow", "pov"].map((v) => (
            <button key={v} className={`btn ${view === v ? "active" : ""}`} onClick={() => changeView(v)}>
              {v === "pov" ? "ROBOT POV" : v.toUpperCase()}
            </button>
          ))}
          <button className="btn" onClick={() => setTrails(!trails)}>PATH TRAILS: {trails ? "ON" : "OFF"}</button>
          <button className="btn" onClick={() => setDash(!dash)}>DASHBOARD: {dash ? "ON" : "OFF"}</button>
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
            <div className="row"><span>Refresh</span><b>2 sec</b></div>
          </div>

          <div className="card">
            <div className="card-title"><span>Event stream</span></div>
            <div className="events">{events.map((x, i) => <div key={i}>• {x}</div>)}</div>
          </div>
        </aside>
      )}

      <div className="legend">
        <span><i className="dot" style={{ background: "#45d5a2" }} />alive</span>
        <span><i className="dot" style={{ background: "#6aaaff" }} />on task</span>
        <span><i className="dot" style={{ background: "#ffc06b" }} />no response</span>
        <span><i className="dot" style={{ background: "#ff7684" }} />dead</span>
        <span><i className="dot" style={{ background: "#5b636f" }} />powered off</span>
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
