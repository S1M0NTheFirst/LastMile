const ORCHESTRATOR_URL = (process.env.ORCHESTRATOR_URL || "http://localhost:8012").replace(/\/$/, "");

export async function POST(req) {
  try {
    const body = await req.json();
    const response = await fetch(`${ORCHESTRATOR_URL}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text || "orchestrator returned a non-JSON error" };
    }
    return Response.json(data, { status: response.status });
  } catch (error) {
    return Response.json({ error: String(error) }, { status: 502 });
  }
}
