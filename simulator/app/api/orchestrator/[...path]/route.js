const ORCHESTRATOR_URL = (process.env.ORCHESTRATOR_URL || "http://localhost:8012").replace(/\/$/, "");

export const dynamic = "force-dynamic";

// Passes other orchestrator calls (camera video, autopilot) through unchanged.
// The body is streamed, not buffered, so live video arrives frame by frame.
async function forward(req, { params }) {
  try {
    const response = await fetch(`${ORCHESTRATOR_URL}/${params.path.map(encodeURIComponent).join("/")}`, {
      method: req.method,
      headers: { "Content-Type": "application/json" },
      body: req.method === "GET" ? undefined : await req.text(),
      cache: "no-store",
    });
    return new Response(response.body, {
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
