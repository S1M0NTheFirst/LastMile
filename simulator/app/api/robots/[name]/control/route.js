import { findRobot } from "../../../../lib/fleet";

// Proxy status/battery control to the robot itself, whether it's a container
// on this machine or a robot on another machine (REMOTE_ROBOTS).
export async function POST(req, { params }) {
  const body = await req.json();
  const robot = await findRobot(params.name);
  if (!robot) return Response.json({ error: "not found" }, { status: 404 });
  if (!robot.running) return Response.json({ error: "powered off" }, { status: 409 });
  try {
    const res = await fetch(`${robot.url}/control`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return Response.json(await res.json());
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 500 });
  }
}
