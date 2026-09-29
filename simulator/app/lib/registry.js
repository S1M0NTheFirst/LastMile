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
    store.next = Math.max(store.next, Number(hint[1]) + 1);
  } else {
    while (store.robots.has(`robot${store.next}`)) store.next++;
    assigned = `robot${store.next++}`;
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
