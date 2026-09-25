import Docker from "dockerode";

// Robots are named `robot` + a number, whether they're plain containers
// (docker compose, single machine) or swarm services (docker stack,
// possibly spread across multiple machines).
const ROBOT_RE = /^robot\d+$/;
const docker = new Docker(); // local Docker socket; on a swarm manager this
                              // also has cluster-wide visibility into services.

async function isSwarmActive() {
  try {
    const info = await docker.info();
    return info?.Swarm?.LocalNodeState === "active";
  } catch {
    return false;
  }
}

// ---- swarm mode: robots are services, possibly running on other nodes ----

function robotIdFromEnv(service) {
  const env = service.Spec?.TaskTemplate?.ContainerSpec?.Env || [];
  const hit = env.find((e) => e.startsWith("ROBOT_ID="));
  return hit ? hit.split("=")[1] : null;
}

function servicePort(service) {
  const ports = service.Endpoint?.Ports || [];
  const hit = ports.find((p) => p.TargetPort === 8000);
  return hit ? hit.PublishedPort : null;
}

export async function getFleet() {
  if (await isSwarmActive()) return getFleetFromSwarm();
  return getFleetFromContainers();
}

async function getFleetFromSwarm() {
  const services = await docker.listServices();
  const robots = services
    .map((s) => ({ service: s, name: robotIdFromEnv(s) }))
    .filter((r) => r.name && ROBOT_RE.test(r.name))
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));

  return Promise.all(
    robots.map(async ({ service, name }) => {
      const port = servicePort(service);
      const replicas = service.Spec?.Mode?.Replicated?.Replicas ?? 0;
      const desiredOn = replicas > 0;
      const entry = {
        name,
        power: desiredOn ? "on" : "off",
        // desired>0 but unreachable usually means no node satisfies the
        // service's placement constraint yet (e.g. no worker has joined).
        dockerStatus: desiredOn ? "pending" : "stopped",
        hostPort: port,
        reachable: false,
        state: null,
      };
      // Reachable from any node's localhost via the swarm routing mesh,
      // regardless of which physical machine actually runs the container.
      if (desiredOn && port) {
        try {
          const res = await fetch(`http://localhost:${port}/state`, {
            signal: AbortSignal.timeout(1500),
            cache: "no-store",
          });
          entry.state = await res.json();
          entry.reachable = true;
          entry.dockerStatus = "running";
        } catch {
          // dead / no_response robots won't answer, or the task hasn't started yet
        }
      }
      return entry;
    })
  );
}

export async function setSwarmPower(name, on) {
  const services = await docker.listServices();
  const hit = services.find((s) => robotIdFromEnv(s) === name);
  if (!hit) return null;
  const service = docker.getService(hit.ID);
  const spec = hit.Spec;
  spec.Mode = { Replicated: { Replicas: on ? 1 : 0 } };
  await service.update({ _query: { version: hit.Version.Index }, _body: spec });
  return { name, power: on ? "on" : "off" };
}

// ---- plain docker compose mode: robots are containers on this machine ----

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

async function getFleetFromContainers() {
  const list = await docker.listContainers({ all: true });
  const robots = list
    .filter((c) => ROBOT_RE.test(containerRobotName(c)))
    .sort((a, b) =>
      containerRobotName(a).localeCompare(containerRobotName(b), undefined, { numeric: true })
    );

  return Promise.all(
    robots.map(async (c) => {
      const name = containerRobotName(c);
      const port = containerPort(c);
      const running = c.State === "running";
      const entry = {
        name,
        power: running ? "on" : "off",
        dockerStatus: c.State,
        hostPort: port,
        reachable: false,
        state: null,
      };
      if (running && port) {
        try {
          const res = await fetch(`http://localhost:${port}/state`, {
            signal: AbortSignal.timeout(1500),
            cache: "no-store",
          });
          entry.state = await res.json();
          entry.reachable = true;
        } catch {
          // dead / no_response robots won't answer
        }
      }
      return entry;
    })
  );
}

// Finds a robot's published host port + whether it's currently powered on,
// regardless of whether it's a swarm service or a plain container.
export async function findRobot(name) {
  if (await isSwarmActive()) {
    const services = await docker.listServices();
    const hit = services.find((s) => robotIdFromEnv(s) === name);
    if (!hit) return null;
    const replicas = hit.Spec?.Mode?.Replicated?.Replicas ?? 0;
    return { port: servicePort(hit), running: replicas > 0 };
  }
  const c = await getContainer(name);
  if (!c) return null;
  return { port: containerPort(c.info), running: c.info.State === "running" };
}

export { isSwarmActive };
