# Next.js web service (M11E). The only public service: the browser talks to
# this and nothing else; it calls the private FastAPI service server-side.
#
# Multi-stage: dependencies, production build, then a runtime image holding the
# build output and production dependencies only, running as a non-root user.
# No secret is baked into any layer. Only the Clerk PUBLISHABLE key (public by
# design, inlined into the browser bundle) is accepted as a build argument;
# CLERK_SECRET_KEY and every other value are supplied at run time.
#
# Build:  docker build -f docker/web.Dockerfile -t ap-agent-web \
#           --build-arg NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=<pk_...> .

FROM node:22-slim AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=""
ENV NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=${NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY}
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ ./
RUN npm run build && npm run check:build-secrets

FROM node:22-slim AS prod-deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM node:22-slim AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
COPY --from=prod-deps /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next
COPY frontend/package.json frontend/next.config.ts ./
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/sign-in').then(r=>process.exit(r.status<500?0:1)).catch(()=>process.exit(1))"]
# On Render the private API address arrives as AP_AGENT_API_HOSTPORT (host:port, from the
# Blueprint's fromService reference); AP_AGENT_API_BASE_URL may be set explicitly instead.
CMD ["sh", "-c", "export AP_AGENT_API_BASE_URL=\"${AP_AGENT_API_BASE_URL:-http://${AP_AGENT_API_HOSTPORT}}\"; exec node_modules/.bin/next start --port ${PORT} --hostname ${HOSTNAME}"]
