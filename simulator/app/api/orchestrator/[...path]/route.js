const ORCHESTRATOR_URL = (process.env.ORCHESTRATOR_URL || "http://localhost:8012").replace(/\/$/, "");

// Passes other orchestrator calls (camera frames, autopilot) through unchanged,
// including binary JPEG bodies.
async function forward(req, { params }) {
  try {
    const response = await fetch(`${ORCHESTRATOR_URL}/${params.path.map(encodeURIComponent).join("/")}`, {
      method: req.method,
      headers: { "Content-Type": "application/json" },
      body: req.method === "GET" ? undefined : await req.text(),
      cache: "no-store",
    });
    return new Response(await response.arrayBuffer(), {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("content-type") || "application/json",
        "Cache-Control": "no-store",
      },
    });
  } catch (error) {
    return Response.json({ error: String(error) }, { status: 502 });
  }
}

export { forward as GET, forward as POST };
