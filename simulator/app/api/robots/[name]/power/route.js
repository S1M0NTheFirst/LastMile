import { getContainer, isSwarmActive, setSwarmPower } from "../../../../lib/fleet";

export async function POST(req, { params }) {
  const { on } = await req.json();

  if (await isSwarmActive()) {
    // Power in swarm mode = scaling the service's replica count 0/1,
    // since the robot may be running on a different physical machine.
    const result = await setSwarmPower(params.name, on);
    if (!result) return Response.json({ error: "not found" }, { status: 404 });
    return Response.json(result);
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
