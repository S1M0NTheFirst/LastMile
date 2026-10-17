import Docker from "dockerode";
import { existsSync } from "node:fs";
import { homedir, networkInterfaces } from "node:os";
import { join } from "node:path";
import { forgetRobot, getRobot, listRobots } from "./registry";

// Every robot, on any machine, is known through the registry. Robots whose
// container runs on this machine's Docker can also be powered on/off.
// Docker Desktop's user-level installation on macOS does not create /var/run/docker.sock.
const userSocket = join(homedir(), ".docker", "run", "docker.sock");
const docker = new Docker(!process.env.DOCKER_HOST && !existsSync("/var/run/docker.sock") && existsSync(userSocket)
  ? { socketPath: userSocket }
  : undefined);
const ONLINE_MS = 10_000; // no heartbeat for this long => robot is offline

function isFresh(robot) {
  return Date.now() - robot.lastSeen < ONLINE_MS;
}

// This machine's own addresses: a robot registered from one of them runs in
// local Docker, so once its container is gone (join.py --stop) it is forgotten.
function localAddresses() {
  return new Set(Object.values(networkInterfaces()).flat().map((i) => i?.address).filter(Boolean));
}

// join.py labels each container with the host:port it registers under.
async function localContainers() {
  try {
    const list = await docker.listContainers({
      all: true,
      filters: { label: ["lastmile.robot"] },
    });
    const byAddr = new Map();
    for (const c of list) {
      byAddr.set(`${c.Labels["lastmile.host"]}:${c.Labels["lastmile.port"]}`, c);
    }
    return byAddr;
  } catch {
    return new Map(); // Docker not running here; remote robots still show
  }
}

function describe(robot, container) {
  const running = container ? container.State === "running" : isFresh(robot);
  // On Docker Desktop, the host cannot always reach its own LAN IP from a
  // local published container port. Use loopback for local containers while
  // preserving the advertised LAN address for remote machines.
  const localUrl = container ? `http://127.0.0.1:${robot.port}` : `http://${robot.host}:${robot.port}`;
  return {
    name: robot.name,
    url: localUrl,
    hostPort: `${robot.host}:${robot.port}`,
    remote: !container,
    power: running ? "on" : "off",
    container,
  };
}

export async function getFleet() {
  const containers = await localContainers();
  const local = localAddresses();
  // Only prune when Docker answered; an unreachable Docker returns no containers.
  const dockerUp = await docker.ping().then(() => true, () => false);
  for (const robot of listRobots()) {
    const removed = dockerUp && local.has(robot.host) && !containers.has(`${robot.host}:${robot.port}`);
    if (removed && !isFresh(robot)) forgetRobot(robot.name);
  }
  return Promise.all(
    listRobots().map(async (robot) => {
      const { container, ...entry } = describe(
        robot,
        containers.get(`${robot.host}:${robot.port}`)
      );
      entry.reachable = false;
      entry.state = null;
      if (entry.power === "on") {
        try {
          const res = await fetch(`${entry.url}/state`, {
            signal: AbortSignal.timeout(1500),
            cache: "no-store",
          });
          entry.state = await res.json();
          entry.reachable = true;
        } catch {
          // dead / no_response robots won't answer
        }
      }
      delete entry.url;
      return entry;
    })
  );
}

// Finds a robot's URL, whether it's on, and (if local) its container handle.
export async function findRobot(name) {
  const robot = getRobot(name);
  if (!robot) return null;
  const containers = await localContainers();
  const { container, ...entry } = describe(robot, containers.get(`${robot.host}:${robot.port}`));
  return {
    url: entry.url,
    running: entry.power === "on",
    handle: container ? docker.getContainer(container.Id) : null,
  };
}
