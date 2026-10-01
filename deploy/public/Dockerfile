# The public portal: the React build served by Caddy, which also proxies the
# portal's API calls to the controller and gets HTTPS certificates from
# Let's Encrypt. Build context: the repository root.
FROM node:22-alpine AS portal
WORKDIR /src/portal
COPY portal/package.json portal/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY portal/ ./
RUN npm run build

FROM caddy:2-alpine
COPY deploy/public/Caddyfile /etc/caddy/Caddyfile
COPY --from=portal /src/portal/dist /srv/portal
