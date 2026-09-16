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
              <div className="hint">battery {Math.round(bat)}% · port {r.hostPort || "—"}</div>

              <div className="row" style={{ marginTop: 10 }}>
                <div className="btns">
                  <button
                    className={r.power === "on" ? "active" : ""}
                    onClick={() => act(() => post(`/api/robots/${r.name}/power`, { on: true }))}
                  >
                    On
                  </button>
                  <button
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
            </div>
          );
        })}
      </div>
    </div>
  );
}
