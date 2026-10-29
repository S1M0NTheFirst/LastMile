import { forgetByAddress, listRobots, register } from "../../lib/registry";

export const dynamic = "force-dynamic";

// Robots look up each other's addresses here.
export async function GET() {
  return Response.json({ robots: listRobots() });
}

// Robots register (and heartbeat) here with the address they're reachable at.
export async function POST(req) {
  const { host, port, name } = await req.json();
  if (!host || !port) {
    return Response.json({ error: "host and port are required" }, { status: 400 });
  }
  return Response.json(register({ host, port: Number(port), name }));
}

// Removes robots whose containers were stopped (join.py --stop sends their host:port).
export async function DELETE(req) {
  const { robots = [] } = await req.json().catch(() => ({}));
  const removed = robots.map((r) => forgetByAddress(r.host, Number(r.port))).filter(Boolean);
  return Response.json({ removed });
}
