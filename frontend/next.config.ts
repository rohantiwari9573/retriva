import type { NextConfig } from "next";

// When set (e.g. on Vercel), /api/* is proxied to this backend origin so the
// browser only ever talks to the frontend's own origin - the backend's
// SameSite=Lax auth cookies then stay first-party instead of cross-site.
const apiProxyTarget = process.env.API_PROXY_TARGET?.replace(/\/+$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
  experimental: {
    // Next buffers request bodies when a proxy file exists, even on paths it does
    // not match, and silently cuts a body off at this limit. It must stay above
    // the API's own cap (max_request_body_bytes: MAX_DOCUMENT_SIZE_MB (25) MiB plus
    // 1 MiB multipart overhead = 26 MiB), so an oversized upload always reaches the
    // API intact enough for it to answer 413. At exactly 26mb a chunked upload was
    // cut to the cap and answered 422 instead. See src/lib/next-config.test.ts.
    proxyClientMaxBodySize: "27mb",
  },
  // Proxy mode: API calls go to relative /api/* paths on this origin.
  ...(apiProxyTarget && !process.env.NEXT_PUBLIC_API_URL
    ? { env: { NEXT_PUBLIC_API_URL: "" } }
    : {}),
  async rewrites() {
    return apiProxyTarget
      ? [{ source: "/api/:path*", destination: `${apiProxyTarget}/api/:path*` }]
      : [];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
