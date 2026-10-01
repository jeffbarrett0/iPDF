// The browser only ever talks to this Next.js server; /api/* is proxied to the local FastAPI process.
const API_URL = process.env.API_URL || `http://127.0.0.1:${process.env.API_PORT || 8000}`;

/** @type {import('next').NextConfig} */
const nextConfig = {
  poweredByHeader: false,
  // Uploads are proxied through Next; without this they are truncated at 10 MB.
  experimental: { middlewareClientMaxBodySize: `${Number(process.env.MAX_UPLOAD_MB || 200) + 10}mb` },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
        ],
      },
    ];
  },
};
export default nextConfig;
