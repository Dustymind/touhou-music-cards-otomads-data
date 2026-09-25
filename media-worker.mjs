/**
 * 素材站的小 Worker 脚本（数据仓库 `wrangler.jsonc` 的 `main`）—— **只接管 `/media/*`**，做两件事：
 *
 * 1. **补上 `Range` 支持**：Workers 静态资源那条路实测**不认 Range**（三种写法都返回 200 + 整份，
 *    而老 Pages 项目给的是 206 + `content-range`）。音频是渐进式 MP3，浏览器靠 `accept-ranges`
 *    决定"能不能跳着取" —— 少了它，随机起播位只能从头下起。
 * 2. **自己带 CORS 与缓存头**：CF 文档明说 `_headers` **不作用于 Worker 生成的响应**，
 *    所以凡是本脚本返回的，都得自己把 `Access-Control-Allow-Origin` 等头带上（清单/响度表那一侧走的
 *    仍是静态资源那条路，由构建产物里的 `_headers` 负责）。
 *
 * 只匹配 `/media/*`（`assets.run_worker_first`）：其余路径**不进脚本** ⇒ 静态资源照旧免请求、走边缘，
 * 只有音频这一类"需要切片"的请求才付一次 Worker 调用的代价。
 *
 * 行为口径：
 * - 没有 `Range`：原样返回静态资源，但显式盖上媒体缓存头（`?v=` 已保证换版本必换 URL）并 advertise
 *   `Accept-Ranges: bytes`；
 * - 有 `Range`：先看资源层认不认（认就沿用它的 206）；不认就自己切（`bytes=a-b` / `bytes=a-` / `bytes=-n`，
 *   多段只取第一段，越界返回 416）；
 * - `HEAD` 只回头、不读正文。
 */

const MEDIA_PREFIX = "/media/";

/** 媒体缓存头：与老站实测一致（地址带 `?v=<逐曲版本>`，版本一变 URL 就变） */
const MEDIA_CACHE = "public, max-age=14400, must-revalidate";

/** 每个响应都要带的头（`_headers` 管不到 Worker 生成的响应，所以在这里补） */
const BASE_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges",
  "X-Content-Type-Options": "nosniff",
  "Accept-Ranges": "bytes",
};

/**
 * `Range` 头 → 闭区间 `[start, end]`（含端点）；不合法 / 越界 / 多段（只取第一段）之外返回 `null`。
 * 与 HTTP 语义一致：`bytes=a-b`、`bytes=a-`（到结尾）、`bytes=-n`（最后 n 字节）。
 */
export function sliceRange(header, size) {
  const matched = /^bytes=(\d*)-(\d*)$/.exec(String(header ?? "").trim().split(",")[0].trim());
  if (!matched || !Number.isFinite(size) || size <= 0) return null;
  const [, startText, endText] = matched;
  if (startText === "" && endText === "") return null;

  if (startText === "") {                       // 后缀写法：最后 n 字节
    const suffix = Number(endText);
    if (!Number.isFinite(suffix) || suffix <= 0) return null;
    return [Math.max(0, size - suffix), size - 1];
  }
  const start = Number(startText);
  if (!Number.isFinite(start) || start >= size) return null;
  if (endText === "") return [start, size - 1];
  const end = Number(endText);
  if (!Number.isFinite(end) || end < start) return null;
  return [start, Math.min(end, size - 1)];      // 超出结尾就夹到结尾
}

function withBaseHeaders(response, extra = {}) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries({ ...BASE_HEADERS, ...extra })) headers.set(name, value);
  return new Response(response.body, {
    status: response.status, statusText: response.statusText, headers,
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith(MEDIA_PREFIX)) return env.ASSETS.fetch(request);

    const range = request.headers.get("Range");
    const asset = await env.ASSETS.fetch(request);

    if (!range) return withBaseHeaders(asset, { "Cache-Control": MEDIA_CACHE });
    if (asset.status === 206) return withBaseHeaders(asset, { "Cache-Control": MEDIA_CACHE });

    // **不能只信 `Content-Length`**：本地 miniflare 那条路给的是流式响应、没有这个头（踩过一次，
    // 结果把 size 当成 0 ⇒ 所有 Range 都回 416）。长度未知时就把正文读进来量 —— 音频 1.7–7 MB，
    // 远低于 128 MB 上限，值得。
    const declared = Number(asset.headers.get("Content-Length") ?? 0);
    const buffered = request.method === "HEAD" ? null : await asset.arrayBuffer();
    const size = declared > 0 ? declared : (buffered ? buffered.byteLength : 0);

    if (size === 0) {
      // 连长度都问不出来（HEAD 且上游没给头）：不假装支持 Range，原样透传那份响应
      return withBaseHeaders(asset, { "Cache-Control": MEDIA_CACHE });
    }
    const span = sliceRange(range, size);
    if (span === null) {
      return new Response(null, {
        status: 416,
        headers: { ...BASE_HEADERS, "Content-Range": `bytes */${size}` },
      });
    }
    const [start, end] = span;
    return new Response(buffered ? buffered.slice(start, end + 1) : null, {
      status: 206,
      headers: {
        ...BASE_HEADERS,
        "Cache-Control": MEDIA_CACHE,
        "Content-Type": asset.headers.get("Content-Type") ?? "application/octet-stream",
        "Content-Range": `bytes ${start}-${end}/${size}`,
        "Content-Length": String(end - start + 1),
      },
    });
  },
};
