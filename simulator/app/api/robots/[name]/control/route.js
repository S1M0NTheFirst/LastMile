import { findRobot } from "../../../../lib/fleet";

// Proxy status/battery control to the robot itself, on whichever machine it
// runs (its address comes from the registry).
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
      signal: AbortSignal.timeout(5000),
    });
    return Response.json(await res.json(), { status: res.status });
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 500 });
  }
}
