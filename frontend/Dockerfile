# Frontend build image. This is a BUILD-ONLY artifact, not a runtime service.
#
# The build stage runs the Vite production build with the public VITE_ vars
# baked into the bundle via build args. The export stage contains nothing but
# the built dist at /dist so the host can extract it (docker create + docker
# cp, see PROD_CONTAINERS.md); host Nginx serves the files and proxies /api.
# No nginx or node runs from this image.

FROM node:20 AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Public build-time configuration, inlined into the bundle by Vite.
# VITE_CLERK_PUBLISHABLE_KEY is required (the app throws at startup without
# it); the VITE_SENTRY_* vars are optional and the SDK stays inert when the
# DSN is empty.
ARG VITE_CLERK_PUBLISHABLE_KEY
ARG VITE_SENTRY_DSN
ARG VITE_SENTRY_ENVIRONMENT
ARG VITE_SENTRY_RELEASE
ARG VITE_SENTRY_SEND_DEFAULT_PII
ARG VITE_SENTRY_TRACES_SAMPLE_RATE
ARG VITE_SENTRY_TRACE_PROPAGATION_TARGETS
ARG VITE_SENTRY_REPLAYS_SESSION_SAMPLE_RATE
ARG VITE_SENTRY_REPLAYS_ON_ERROR_SAMPLE_RATE
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY \
    VITE_SENTRY_DSN=$VITE_SENTRY_DSN \
    VITE_SENTRY_ENVIRONMENT=$VITE_SENTRY_ENVIRONMENT \
    VITE_SENTRY_RELEASE=$VITE_SENTRY_RELEASE \
    VITE_SENTRY_SEND_DEFAULT_PII=$VITE_SENTRY_SEND_DEFAULT_PII \
    VITE_SENTRY_TRACES_SAMPLE_RATE=$VITE_SENTRY_TRACES_SAMPLE_RATE \
    VITE_SENTRY_TRACE_PROPAGATION_TARGETS=$VITE_SENTRY_TRACE_PROPAGATION_TARGETS \
    VITE_SENTRY_REPLAYS_SESSION_SAMPLE_RATE=$VITE_SENTRY_REPLAYS_SESSION_SAMPLE_RATE \
    VITE_SENTRY_REPLAYS_ON_ERROR_SAMPLE_RATE=$VITE_SENTRY_REPLAYS_ON_ERROR_SAMPLE_RATE

# Build-only secrets for Sentry source map upload (consumed by vite.config.ts
# via process.env). These are NOT VITE_ vars, so Vite never inlines them into
# the bundle, and they are used only in this stage, so they never appear in
# the export image or its history.
ARG SENTRY_AUTH_TOKEN
ARG SENTRY_ORG
ARG SENTRY_PROJECT
ENV SENTRY_AUTH_TOKEN=$SENTRY_AUTH_TOKEN \
    SENTRY_ORG=$SENTRY_ORG \
    SENTRY_PROJECT=$SENTRY_PROJECT

# Source map upload to Sentry (when SENTRY_AUTH_TOKEN is set) happens during
# this build, while the .map files still exist.
RUN npm run build

# Strip source maps from the served output: they were already uploaded to
# Sentry above, and shipping them would expose the full source publicly.
# To serve maps publicly instead, delete this single RUN line.
RUN find dist -type f -name '*.map' -delete


# Pure artifact stage: only the built dist, at a fixed path for extraction.
FROM scratch AS dist

COPY --from=build /app/dist /dist
