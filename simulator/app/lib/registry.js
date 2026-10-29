// In-memory robot registry: hands out names first-come-first-served
// (robot0, robot1, ...) and tracks where each robot can be reached.
// Kept on globalThis so it survives Next.js dev-mode module reloads.
const store = (globalThis.__lastmileRegistry ??= { robots: new Map(), next: 0 });

const NAME_RE = /^robot(\d+)$/;

function keyOf(host, port) {
  return `${host}:${port}`;
}

// Registration doubles as the heartbeat: robots call it every few seconds.
// A robot that re-registers from the same host:port keeps its name; after a
// registry restart, robots send their old name as a hint and get it back.
export function register({ host, port, name }) {
  const key = keyOf(host, port);
  for (const r of store.robots.values()) {
    if (keyOf(r.host, r.port) === key) {
      r.lastSeen = Date.now();
      return r;
    }
  }

  const hint = NAME_RE.exec(name || "");
  let assigned;
  if (hint && !store.robots.has(name)) {
    assigned = name;
  } else {
    // The lowest free number, so names start again at robot0 after the robots are cleared.
    let n = 0;
    while (store.robots.has(`robot${n}`)) n++;
    assigned = `robot${n}`;
  }

  const robot = { name: assigned, host, port, lastSeen: Date.now() };
  store.robots.set(assigned, robot);
  return robot;
}

export function listRobots() {
  return [...store.robots.values()].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, { numeric: true })
  );
}

export function getRobot(name) {
  return store.robots.get(name) || null;
}

// Drops a robot whose container was removed, so it stops showing as "off".
export function forgetRobot(name) {
  store.robots.delete(name);
}

// Drops the robot registered at host:port; returns its name, or null if there was none.
export function forgetByAddress(host, port) {
  for (const r of store.robots.values()) {
    if (r.host === host && r.port === port) {
      store.robots.delete(r.name);
      return r.name;
    }
  }
  return null;
}
