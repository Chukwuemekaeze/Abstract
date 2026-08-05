// Shared secret redaction (frontend mirror of backend app/utils/redaction.py).
//
// A single best-effort scrubber for secret shapes that could appear verbatim in
// text we did not author — an error message or exception value that happens to
// carry a token or inline credential. The Sentry `beforeSend` hook calls
// `redactSecrets` so there is one source of truth for what a secret looks like,
// the same way the backend log sink and Sentry `before_send` both call it.
//
// These are a safety net, not a guarantee: they only catch the shapes listed
// here. Our own code avoids putting secrets into messages in the first place.

// GitHub tokens (ghp_/gho_/ghs_/ghr_/ghu_ ...).
const GITHUB_TOKEN = /\bgh[posru]_[A-Za-z0-9]{20,}\b/g
const BEARER = /(authorization:\s*bearer\s+)\S+/gi
// The password half of a `user:password` credential fed to chpasswd. Passwords
// never contain whitespace, quotes, or a pipe, so the match stops cleanly at the
// shell delimiter; the length floor avoids masking unix `user:group` pairs.
const CHPASSWD_CRED = /(\b[\w.-]+:)([^\s'"|]{8,})/g

// Mask known secret shapes in a message, preserving surrounding context so the
// line still shows what happened. Best-effort: only the patterns above are covered.
export function redactSecrets(message: string): string {
  let out = message
  if (out.includes('chpasswd')) {
    out = out.replace(CHPASSWD_CRED, '$1***')
  }
  out = out.replace(GITHUB_TOKEN, '***')
  out = out.replace(BEARER, '$1***')
  return out
}
