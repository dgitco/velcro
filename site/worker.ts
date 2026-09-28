// velcro.dgit.co is static; this runs only for the install and download paths and counts them per
// day in the shared D1 dgit-stats (product "velcro"). The counts are private: nothing serves them.
//   latest    the installer starting (it reads /latest first), either mode
//   cli-only  the installer fetching the bare command (--cli-only)
//   zip       the app zip (from the installer, or a browser download)
//   install   the installer script itself, e.g. someone reading it in a browser
// client is "browser" for Mozilla user agents and "cli" otherwise (curl).

interface D1Statement { bind(...values: unknown[]): D1Statement; run(): Promise<unknown> }
interface Env {
  ASSETS: { fetch(request: Request): Promise<Response> };
  STATS: { prepare(sql: string): D1Statement };
}
interface Ctx { waitUntil(promise: Promise<unknown>): void }

const BOT = /bot|crawl|spider|slurp|preview|monitor|headless/i;

function itemOf(path: string): string | null {
  if (path === "/latest") return "latest";
  if (path === "/velcro") return "cli-only";
  if (path === "/install") return "install";
  if (/^\/velcro-[0-9.]+\.zip$/.test(path)) return "zip";
  return null;
}

export default {
  async fetch(request: Request, env: Env, ctx: Ctx): Promise<Response> {
    const response = await env.ASSETS.fetch(request);
    const item = itemOf(new URL(request.url).pathname);
    const ua = request.headers.get("user-agent") ?? "";
    if (item && request.method === "GET" && response.ok && !BOT.test(ua)) {
      const client = ua.includes("Mozilla") ? "browser" : "cli";
      ctx.waitUntil(
        env.STATS.prepare(
          "INSERT INTO hits (day, product, item, client, n) VALUES (?, 'velcro', ?, ?, 1) ON CONFLICT (day, product, item, client) DO UPDATE SET n = n + 1",
        )
          .bind(new Date(Date.now() + 9 * 3600_000).toISOString().slice(0, 10), item, client)
          .run()
          .catch(() => {}),
      );
    }
    return response;
  },
};
