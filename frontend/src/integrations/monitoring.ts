// Application monitoring: error tracking, tracing, and session replay.
//
// The single boundary to Sentry (frontend mirror of backend
// app/integrations/monitoring.py). This is the only module in the app that
// imports `@sentry/react`; everything else calls the provider-agnostic helpers
// below. Swapping Sentry for another provider means rewriting this file alone —
// no call site changes.
//
// `initMonitoring` is called once, at the top of `main.tsx`, before the app
// mounts. Page loads, navigations, and fetch/XHR calls are auto-instrumented by
// the SDK once initialized (via the React Router v7 tracing integration, wired
// in App.tsx); the helpers here cover the explicit surface — capturing errors,
// attributing events to a user, and reporting standalone messages.

import * as Sentry from '@sentry/react'
import { useEffect } from 'react'
import {
  createRoutesFromChildren,
  matchRoutes,
  useLocation,
  useNavigationType,
} from 'react-router-dom'

import { redactSecrets } from '@/lib/redaction'

const env = import.meta.env

// Parse a numeric env override, falling back to `fallback` when unset/invalid.
function num(value: string | undefined, fallback: number): number {
  const parsed = value === undefined ? NaN : Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function bool(value: string | undefined): boolean {
  return value === 'true' || value === '1'
}

// Parse a comma-separated env list, falling back to `fallback` when unset/empty.
function list(value: string | undefined, fallback: string[]): string[] {
  const parts = (value ?? '').split(',').map((s) => s.trim()).filter(Boolean)
  return parts.length ? parts : fallback
}

// Initialize Sentry from Vite env. Safe to call once at startup.
//
// With no DSN configured the SDK is never initialized (helpers become no-ops),
// so dev and test environments need no special casing — the same inert-by-default
// contract as the backend's sentry_dsn.
export function initMonitoring(): void {
  const dsn = env.VITE_SENTRY_DSN
  if (!dsn) {
    return
  }

  Sentry.init({
    dsn,
    environment: env.VITE_SENTRY_ENVIRONMENT ?? 'development',
    release: env.VITE_SENTRY_RELEASE || undefined,
    // Whether to attach PII (client IP, etc.). Off by default; opt in per
    // environment. Secrets are scrubbed via beforeSend either way.
    sendDefaultPii: bool(env.VITE_SENTRY_SEND_DEFAULT_PII),
    integrations: [
      // Names navigation/pageload transactions after the matched route pattern
      // (e.g. /servers/:id) instead of the raw URL. Paired with
      // wrapReactRouterRouting around <Routes> in App.tsx.
      Sentry.reactRouterBrowserTracingIntegration({
        useEffect,
        useLocation,
        useNavigationType,
        createRoutesFromChildren,
        matchRoutes,
      }),
      // Records a DOM replay of the session leading up to an error, with all text
      // and inputs masked so nothing sensitive is captured.
      Sentry.replayIntegration({ maskAllText: true, maskAllInputs: true }),
    ],
    // Fraction of requests turned into performance transactions.
    tracesSampleRate: num(env.VITE_SENTRY_TRACES_SAMPLE_RATE, 1.0),
    // Distributed tracing: attach the sentry-trace/baggage headers to these
    // outgoing requests so a frontend transaction links to the backend
    // transaction it triggers (the backend's Sentry integration continues the
    // trace from those headers). Defaults to the same-origin /api calls the axios
    // client makes; override with a comma-separated list for a cross-origin API.
    tracePropagationTargets: list(env.VITE_SENTRY_TRACE_PROPAGATION_TARGETS, ['/api']),
    // Sample a small share of ordinary sessions for replay; always capture the
    // replay when an error occurs.
    replaysSessionSampleRate: num(env.VITE_SENTRY_REPLAYS_SESSION_SAMPLE_RATE, 0.1),
    replaysOnErrorSampleRate: num(env.VITE_SENTRY_REPLAYS_ON_ERROR_SAMPLE_RATE, 1.0),
    // Scrub known secret shapes from every outgoing event, mirroring the backend.
    beforeSend,
  })
}

// Mask secrets in the message and exception values of an event before it leaves.
// Reuses the same `redactSecrets` scrubber as the backend so a token or inline
// credential that reaches Sentry is masked the same way it would be in the logs.
function beforeSend(event: Sentry.ErrorEvent): Sentry.ErrorEvent {
  if (typeof event.message === 'string') {
    event.message = redactSecrets(event.message)
  }
  for (const exception of event.exception?.values ?? []) {
    if (typeof exception.value === 'string') {
      exception.value = redactSecrets(exception.value)
    }
  }
  return event
}

// Report an exception, tagging it with any provided key/value context.
export function captureException(error: unknown, context?: Record<string, unknown>): void {
  Sentry.captureException(error, context ? { extra: context } : undefined)
}

// Report a standalone message at the given severity level.
export function captureMessage(
  message: string,
  level: Sentry.SeverityLevel = 'info',
  context?: Record<string, unknown>,
): void {
  Sentry.captureMessage(message, context ? { level, extra: context } : level)
}

// Attribute subsequent events to a user, mirroring the backend's log/Sentry
// correlation. Uses the same Postgres user id the backend tags its events with.
export function setUser(userId: string): void {
  Sentry.setUser({ id: userId })
}

// Detach the current user from the scope.
export function clearUser(): void {
  Sentry.setUser(null)
}
