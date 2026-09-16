/** @type {import('next').NextConfig} */
const nextConfig = {
  experimental: {
    // dockerode is server-only; keep it out of the client/webpack bundle.
    serverComponentsExternalPackages: ["dockerode"],
  },
};
module.exports = nextConfig;
