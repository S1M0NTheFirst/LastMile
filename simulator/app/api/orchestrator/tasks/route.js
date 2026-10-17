const ORCHESTRATOR_URL = (process.env.ORCHESTRATOR_URL || "http://localhost:8012").replace(/\/$/, "");

export async function GET() {
  try {
    const response = await fetch(`${ORCHESTRATOR_URL}/tasks`, { cache: "no-store" });
    return Response.json(await response.json(), { status: response.status });
  } catch (error) {
    return Response.json({ error: String(error) }, { status: 502 });
  }
}
