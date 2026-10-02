# Next.js frontend as an AWS Lambda container (M11E.1).
#
# Standalone production output + the AWS Lambda Web Adapter behind a public Function URL. The only build
# argument is the Clerk PUBLISHABLE key (public by design, inlined into the browser bundle). Every secret
# (Clerk secret key, CSRF secret) is fetched from SSM Parameter Store at start-up by lambda/entrypoint.mjs
# using the function's execution role -- never a build argument, never in a layer.
#
# Build (from the repository root):
#   docker build -f docker/web.lambda.Dockerfile -t ap-agent-web-lambda \
#     --build-arg NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=<pk_...> .

FROM node:22-slim AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 AP_AGENT_NEXT_OUTPUT=standalone
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=""
ENV NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=${NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY}
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ ./
RUN npm run build && npm run check:build-secrets

FROM node:22-slim AS runtime

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    AWS_LWA_PORT=3000 \
    AWS_LWA_READINESS_CHECK_PATH=/icon.svg \
    AWS_LWA_INVOKE_MODE=buffered

WORKDIR /app
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY frontend/lambda/ ./

CMD ["node", "entrypoint.mjs"]
