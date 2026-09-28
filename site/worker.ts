// velcro.dgit.co is static; this worker only runs for the downloads, to count them (privately, in
// the shared D1 dgit-stats, product "velcro"). Nothing here serves the counts.
//   latest  the installer's first request: one per `curl … /install | sh` run, installs and updates
//   cli     the velcro command on its own
//   zip     the app, from the installer (cli) or the Download button (browser)

interface D1Statement { bind(...values: unknown[]): D1Statement; run(): Promise<unknown> }
interface Env {
  ASSETS: { fetch(request: Request): Promise<Response> };
  STATS: { prepare(sql: string): D1Statement };
}
interface Ctx { waitUntil(promise: Promise<unknown>): void }

const BOT = /bot|crawl|spider|slurp|preview|monitor|headless/i;

function itemOf(path: string): string | null {
  if (path === "/latest") return "latest";
  if (path === "/velcro") return "cli";
  if (/^\/velcro-[\w.-]+\.zip$/.test(path)) return "zip";
  return null;
}

export default {
  async fetch(request: Request, env: Env, ctx: Ctx): Promise<Response> {
    const response = await env.ASSETS.fetch(request);
    const item = itemOf(new URL(request.url).pathname);
    const ua = request.headers.get("user-agent") ?? "";
    if (item && request.method === "GET" && response.status === 200 && !BOT.test(ua)) {
      const client = ua.includes("Mozilla") ? "browser" : "cli";
      const day = new Date(Date.now() + 9 * 3600_000).toISOString().slice(0, 10);
      ctx.waitUntil(
        env.STATS.prepare(
          "INSERT INTO hits (day, product, item, client, n) VALUES (?, 'velcro', ?, ?, 1) ON CONFLICT (day, product, item, client) DO UPDATE SET n = n + 1",
        )
          .bind(day, item, client)
          .run()
          .catch(() => {}),
      );
    }
    return response;
  },
};
