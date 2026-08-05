"""Third-party service integrations.

Each module here is the single boundary to one external provider: `monitoring` (Sentry
error tracking) and `external_logging` (remote log shipping, today BetterStack), with
more (e.g. payments) to come. The rest of the app calls the thin helpers these modules
expose rather than importing vendor SDKs directly, so switching a provider means
rewriting one file, not every call site.
"""
