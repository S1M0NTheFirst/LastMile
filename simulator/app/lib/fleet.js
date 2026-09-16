import Docker from "dockerode";

// Robots are the containers whose names are `robot` + a number.
const ROBOT_RE = /^robot\d+$/;
const docker = new Docker(); // uses the local Docker socket

function robotName(c) {
  // container Names look like ["/robot1"]
  return (c.Names?.[0] || "").replace(/^\//, "");
}

function hostPort(c) {
  const p = (c.Ports || []).find((x) => x.PrivatePort === 8000 && x.PublicPort);
  return p ? p.PublicPort : null;
}

export async function getContainer(name) {
  const list = await docker.listContainers({ all: true });
  const found = list.find((c) => robotName(c) === name);
  if (!found) return null;
  return { info: found, handle: docker.getContainer(found.Id) };
}

// Registry + live state for every robot.
export async function getFleet() {
  const list = await docker.listContainers({ all: true });
  const robots = list
    .filter((c) => ROBOT_RE.test(robotName(c)))
    .sort((a, b) => robotName(a).localeCompare(robotName(b), undefined, { numeric: true }));

  return Promise.all(
    robots.map(async (c) => {
      const name = robotName(c);
      const port = hostPort(c);
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
