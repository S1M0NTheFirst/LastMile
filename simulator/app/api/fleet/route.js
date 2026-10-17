import { getFleet } from "../../lib/fleet";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const robots = await getFleet();
    return Response.json({ robots });
  } catch (e) {
    return Response.json({ error: String(e) }, { status: 500 });
  }
}
