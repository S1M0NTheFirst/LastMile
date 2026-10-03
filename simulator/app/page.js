"use client";

import { useEffect, useState } from "react";

const STATUS_COLORS = {
  alive: "#3ddc84",
  no_response: "#f5a623",
  dead: "#ff5c5c",
};

function robotColor(r) {
  if (r.power === "off") return "#5b636f";
  if (!r.reachable) return "#f5a623"; // running but not answering
  return STATUS_COLORS[r.state?.status] || "#3ddc84";
}

async function post(url, body) {
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export default function Page() {
  const [robots, setRobots] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);
  const [goal, setGoal] = useState("");
  const [task, setTask] = useState(null);
  const [taskMessage, setTaskMessage] = useState("");
  const [taskError, setTaskError] = useState(null);
  const [submitting, setSubmitting] = useState(false);

  async function refresh() {
    try {
      const res = await fetch("/api/fleet", { cache: "no-store" });
      const data = await res.json();
      if (data.error) setError(data.error);
      else {
        setError(null);
        setRobots(data.robots);
      }
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    if (!task?.task_id || ["completed", "failed", "cancelled", "reassigned"].includes(task.status)) return;
    const id = setInterval(async () => {
      try {
        const res = await fetch(`/api/orchestrator/tasks/${task.task_id}`, { cache: "no-store" });
        if (res.ok) setTask(await res.json());
      } catch {
        // The task remains visible while the orchestrator is temporarily unavailable.
      }
    }, 1000);
    return () => clearInterval(id);
  }, [task?.task_id, task?.status]);

  async function submitGoal(event) {
    event.preventDefault();
    if (!goal.trim() || submitting) return;
    setSubmitting(true);
    setTaskError(null);
    setTaskMessage("");
    try {
      const res = await fetch("/api/orchestrator/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: goal.trim() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || "Task request failed");
      setTaskMessage(data.message || "Gemini processed the request.");
      setTask(data.executed || null);
      setGoal("");
      refresh();
    } catch (e) {
      setTaskError(String(e.message || e));
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelCurrentTask() {
    if (!task?.task_id) return;
    try {
      const res = await fetch(`/api/orchestrator/tasks/${task.task_id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reason: "cancelled from dashboard" }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.error || "Cancellation failed");
      setTask(data);
    } catch (e) {
      setTaskError(String(e.message || e));
    }
  }

  async function act(fn) {
    await fn();
    refresh();
  }

  return (
    <div className="wrap">
      <div className="map-panel">
        <h1>LastMile · Robot Swarm Simulator</h1>
        {error && <p className="hint">Orchestrator error: {error}</p>}
        <div className="map">
          {robots.map((r) => {
            const pos = r.state?.position || { x: 50, y: 50 };
            return (
              <div
                key={r.name}
                className="dot"
                onClick={() => setSelected(r.name)}
                title={r.name}
                style={{
                  left: `${pos.x}%`,
                  top: `${100 - pos.y}%`,
                  background: robotColor(r),
                  outline: selected === r.name ? "2px solid #4f8cff" : "none",
                }}
              >
                {r.name.replace("robot", "R")}
              </div>
            );
          })}
        </div>
        <p className="hint">
          Green = alive · Orange = no response · Red = dead · Grey = powered off.
          Positions come from each robot container. Refreshes every 2s.
        </p>
      </div>

      <div className="side">
        <div className="task-console">
          <h2>LLM Task Console</h2>
          <form onSubmit={submitGoal}>
            <textarea
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              placeholder="Deliver package P123 to the north entrance"
              rows={3}
            />
            <div className="hint task-locations">
              Locations: north/east/west/south entrance · warehouse · main door
            </div>
            <button className="submit" disabled={submitting || !goal.trim()}>
              {submitting ? "Thinking…" : "Send task"}
            </button>
          </form>
          {taskError && <p className="task-error">{taskError}</p>}
          {taskMessage && <p className="task-message">{taskMessage}</p>}
          {task && (
            <div className="task-detail">
              <div className="row"><span>Task</span><code>{task.task_id}</code></div>
              <div className="row"><span>Robot</span><strong>{task.robot_id}</strong></div>
              {task.type === "deliver_package" && (
                <>
                  <div className="row"><span>Package</span><strong>{task.payload?.package_id}</strong></div>
                  <div className="row"><span>Destination</span><strong>{task.destination?.name || task.payload?.destination?.name}</strong></div>
                </>
              )}
              <div className="row"><span>Status</span><span className={`task-status ${task.status}`}>{task.status}</span></div>
              <div className="task-progress"><div style={{ width: `${Math.round((task.progress || 0) * 100)}%` }} /></div>
              {!["completed", "failed", "cancelled", "reassigned"].includes(task.status) && (
                <button className="cancel" onClick={cancelCurrentTask}>Cancel task</button>
              )}
              {task.replacement_task_id && (
                <div className="hint" style={{ marginTop: 6 }}>
                  reassigned to {task.replacement_robot_id} · {task.replacement_task_id}
                </div>
              )}
              {task.error && <div className="task-error" style={{ marginTop: 6 }}>{task.error}</div>}
            </div>
          )}
        </div>
        <h2>Fleet ({robots.length})</h2>
        {robots.map((r) => {
          const bat = r.state?.battery ?? 0;
          const status = r.power === "off" ? "off" : r.reachable ? r.state?.status : "no_response";
          return (
            <div
              key={r.name}
              className={`card ${selected === r.name ? "selected" : ""}`}
              onClick={() => setSelected(r.name)}
            >
              <div className="row">
                <span className="name">{r.name}</span>
                <span className="pill" style={{ background: robotColor(r), color: "#0f1115" }}>
                  {status}
                </span>
              </div>

              <div className="bat-track">
                <div
                  className="bat-fill"
                  style={{
                    width: `${bat}%`,
                    background: bat > 30 ? "#3ddc84" : "#ff5c5c",
                  }}
                />
              </div>
              <div className="hint">
                battery {Math.round(bat)}% · {r.remote ? "remote" : "local"} {r.hostPort || "—"}
                <br />
                mode: {r.state?.mode || "unknown"} · accepts tasks: {r.state?.accepting_tasks ? "yes" : "no"}
              </div>

              <div className="row" style={{ marginTop: 10 }}>
                <div className="btns">
                  <button
                    disabled={r.remote}
                    className={r.power === "on" ? "active" : ""}
                    onClick={() => act(() => post(`/api/robots/${r.name}/power`, { on: true }))}
                  >
                    On
                  </button>
                  <button
                    disabled={r.remote}
                    className={r.power === "off" ? "active" : ""}
                    onClick={() => act(() => post(`/api/robots/${r.name}/power`, { on: false }))}
                  >
                    Off
                  </button>
                </div>
              </div>

              <div className="btns" style={{ marginTop: 6 }}>
                {["alive", "no_response", "dead"].map((s) => (
                  <button
                    key={s}
                    disabled={r.power === "off"}
                    className={r.reachable && r.state?.status === s ? "active" : ""}
                    onClick={() => act(() => post(`/api/robots/${r.name}/control`, { status: s }))}
                  >
                    {s}
                  </button>
                ))}
                <button
                  disabled={r.power === "off"}
                  onClick={() => act(() => post(`/api/robots/${r.name}/control`, { battery: 100 }))}
                >
                  charge
                </button>
                <button
                  disabled={r.power === "off"}
                  onClick={() => act(() => post(`/api/robots/${r.name}/control`, { battery: 10 }))}
                >
                  drain
                </button>
              </div>

              <div className="hint" style={{ marginTop: 10 }}>
                <div>messages ({r.state?.inbox?.length || 0})</div>
                {(r.state?.inbox || []).slice(-3).reverse().map((message, index) => (
                  <div key={`${message.ts}-${index}`}>
                    {message.from}: {message.body?.event || message.body?.msg || JSON.stringify(message.body)}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
