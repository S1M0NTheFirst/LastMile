import { listRobots, register } from "../../lib/registry";

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
