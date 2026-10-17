import { findRobot } from "../../../../lib/fleet";

export async function POST(req, { params }) {
  try {
    const body = await req.json();
    const robot = await findRobot(params.name);
    if (!robot) return Response.json({ error: "Robot not found" }, { status: 404 });
    if (!robot.running) return Response.json({ error: "Robot is powered off" }, { status: 409 });
    const res = await fetch(`${robot.url}/navigate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(5000),
    });
    return Response.json(await res.json(), { status: res.status });
  } catch (error) {
    return Response.json({ error: `Navigation request failed: ${error.message}` }, { status: 502 });
  }
}
