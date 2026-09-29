import { findRobot, getContainer } from "../../../../lib/fleet";

export async function POST(req, { params }) {
  const { on } = await req.json();

  // Power = starting/stopping the container, which only works for robots on
  // this machine's Docker. Remote robots are powered from their own machine.
  const robot = await findRobot(params.name);
  if (robot?.remote) {
    return Response.json(
      { error: "remote robot: power it on/off from its own machine" },
      { status: 409 }
    );
  }

  const c = await getContainer(params.name);
  if (!c) return Response.json({ error: "not found" }, { status: 404 });
  try {
    if (on) await c.handle.start();
    else await c.handle.stop();
  } catch (e) {
    // start on running / stop on stopped throws 304-ish; treat as no-op
    if (!String(e).includes("304")) {
      return Response.json({ error: String(e) }, { status: 500 });
    }
  }
  return Response.json({ name: params.name, power: on ? "on" : "off" });
}
