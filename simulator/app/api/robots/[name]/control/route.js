import { getContainer } from "../../../../lib/fleet";

function hostPort(info) {
  const p = (info.Ports || []).find((x) => x.PrivatePort === 8000 && x.PublicPort);
  return p ? p.PublicPort : null;
}

// Proxy status/battery control to the robot container itself.
export async function POST(req, { params }) {
  const body = await req.json();
  const c = await getContainer(params.name);
  if (!c) return Response.json({ error: "not found" }, { status: 404 });
  if (c.info.State !== "running") {
    return Response.json({ error: "powered off" }, { status: 409 });
  }
  const port = hostPort(c.info);
  try {
    const res = await fetch(`http://localhost:${port}/control`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return Response.json(await res.json());
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 500 });
  }
}
