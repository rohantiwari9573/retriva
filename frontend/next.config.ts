import type { NextConfig } from "next";

// When set (e.g. on Vercel), /api/* is proxied to this backend origin so the
// browser only ever talks to the frontend's own origin - the backend's
// SameSite=Lax auth cookies then stay first-party instead of cross-site.
const apiProxyTarget = process.env.API_PROXY_TARGET?.replace(/\/+$/, "");

const nextConfig: NextConfig = {
  output: "standalone",
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
