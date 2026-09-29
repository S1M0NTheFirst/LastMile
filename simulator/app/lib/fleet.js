import Docker from "dockerode";

// Local robots are containers named `robot` + a number on this machine.
// Remote robots run on other machines and are listed in REMOTE_ROBOTS as
// "name=host:port" pairs, e.g. "robot6=192.168.1.50:8006,robot7=...".
const ROBOT_RE = /^robot\d+$/;
const docker = new Docker(); // local Docker socket

function remoteRobots() {
  return (process.env.REMOTE_ROBOTS || "")
    .split(",")
    .map((e) => e.trim())
    .filter(Boolean)
    .map((e) => {
      const [name, addr] = e.split("=").map((s) => s.trim());
      return { name, addr };
    })
    .filter((r) => r.name && r.addr);
}

async function fetchState(base) {
  const res = await fetch(`${base}/state`, {
    signal: AbortSignal.timeout(1500),
    cache: "no-store",
  });
  return res.json();
}

// ---- local robots: containers on this machine ----

function containerRobotName(c) {
  return (c.Names?.[0] || "").replace(/^\//, "");
}

function containerPort(c) {
  const p = (c.Ports || []).find((x) => x.PrivatePort === 8000 && x.PublicPort);
  return p ? p.PublicPort : null;
}

export async function getContainer(name) {
  const list = await docker.listContainers({ all: true });
  const found = list.find((c) => containerRobotName(c) === name);
  if (!found) return null;
  return { info: found, handle: docker.getContainer(found.Id) };
}

async function getLocalFleet() {
  const list = await docker.listContainers({ all: true });
  return Promise.all(
    list
      .filter((c) => ROBOT_RE.test(containerRobotName(c)))
      .map(async (c) => {
        const port = containerPort(c);
        const running = c.State === "running";
        const entry = {
          name: containerRobotName(c),
          power: running ? "on" : "off",
          dockerStatus: c.State,
          hostPort: port,
          remote: false,
          reachable: false,
          state: null,
        };
        if (running && port) {
          try {
            entry.state = await fetchState(`http://localhost:${port}`);
            entry.reachable = true;
          } catch {
            // dead / no_response robots won't answer
          }
        }
        return entry;
      })
  );
}

// ---- remote robots: on another machine, reached over the LAN ----

async function getRemoteFleet() {
  return Promise.all(
    remoteRobots().map(async ({ name, addr }) => {
      // We can't see the other machine's Docker, so an unreachable remote
      // robot shows as powered-on but not answering.
      const entry = {
        name,
        power: "on",
        dockerStatus: "remote",
        hostPort: addr,
        remote: true,
        reachable: false,
        state: null,
      };
      try {
        entry.state = await fetchState(`http://${addr}`);
        entry.reachable = true;
      } catch {
        // offline, dead, or the other machine is unreachable
      }
      return entry;
    })
  );
}

export async function getFleet() {
  const [local, remote] = await Promise.all([getLocalFleet(), getRemoteFleet()]);
  return [...local, ...remote].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, { numeric: true })
  );
}

// Finds a robot's base URL + whether it's powered on, local or remote.
export async function findRobot(name) {
  const remote = remoteRobots().find((r) => r.name === name);
  if (remote) return { url: `http://${remote.addr}`, running: true, remote: true };
  const c = await getContainer(name);
  if (!c) return null;
  const port = containerPort(c.info);
  return {
    url: port ? `http://localhost:${port}` : null,
    running: c.info.State === "running",
    remote: false,
  };
}
