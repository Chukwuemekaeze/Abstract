"""Third-party service integrations.

Each module here is the single boundary to one external provider (Sentry, and later
payments, BetterStack logging). The rest of the app calls the thin helpers these
modules expose rather than importing vendor SDKs directly, so switching a provider
means rewriting one file, not every call site.
"""
