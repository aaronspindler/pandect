import { Agent, getAgentByName } from "agents";
import { investigate } from "./investigator.js";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export class Investigation extends Agent {
  async step(job) {
    const origin = new URL(this.env.PANDECT_ORIGIN);
    if (origin.protocol !== "https:" || origin.origin !== this.env.PANDECT_ORIGIN) {
      throw new Error("Invalid callback configuration");
    }
    const callback = new URL(
      `/cases/${job.case}/chat/agent/${job.turn}/`,
      origin,
    );
    const call = async (operation, parameters) => {
      const response = await fetch(callback, {
        method: "POST",
        redirect: "manual",
        headers: {
          authorization: `Bearer ${job.capability}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({ operation, ...parameters }),
        signal: AbortSignal.timeout(180000),
      });
      if (!response.ok) throw new Error("Callback failed");
      return response.json();
    };
    await investigate(job.stage, call);
  }
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST" || new URL(request.url).pathname !== "/step") {
      return new Response(null, { status: 404 });
    }
    const auth = request.headers.get("authorization") ?? "";
    const encoder = new TextEncoder();
    const expected = encoder.encode(`Bearer ${env.PANDECT_AGENT_SECRET}`);
    const supplied = encoder.encode(auth);
    if (!env.PANDECT_AGENT_SECRET || env.PANDECT_AGENT_SECRET.length < 32 ||
        supplied.length !== expected.length || !crypto.subtle.timingSafeEqual(supplied, expected)) {
      return new Response(null, { status: 403 });
    }
    if (Number(request.headers.get("content-length") ?? Infinity) > 4096) {
      return new Response(null, { status: 413 });
    }
    try {
      const job = await request.json();
      if (!uuid.test(job.case) || !uuid.test(job.turn) ||
          typeof job.capability !== "string" || job.capability.length > 2048) {
        return new Response(null, { status: 400 });
      }
      const instance = await getAgentByName(env.Investigation, job.turn);
      await instance.step(job);
      return new Response(null, { status: 204 });
    } catch {
      return new Response(null, { status: 503 });
    }
  },
};
