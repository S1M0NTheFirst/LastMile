import { findRobot } from "../../../../lib/fleet";

// Power = starting/stopping the container, which only works for robots whose
// container runs on this machine's Docker.
export async function POST(req, { params }) {
  const { on } = await req.json();
  const robot = await findRobot(params.name);
  if (!robot) return Response.json({ error: "not found" }, { status: 404 });
  if (!robot.handle) {
    return Response.json(
      { error: "remote robot: power it on/off from its own machine" },
      { status: 409 }
    );
  }
  try {
    if (on) await robot.handle.start();
    else await robot.handle.stop();
  } catch (e) {
    // start on running / stop on stopped throws 304-ish; treat as no-op
    if (!String(e).includes("304")) {
      return Response.json({ error: String(e) }, { status: 500 });
    }
  }
  return Response.json({ name: params.name, power: on ? "on" : "off" });
}
