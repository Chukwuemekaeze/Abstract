"""Slug derivation for project names.

Slugs end up in VPS file paths (~/.ssh/{slug}-deploy), SSH host aliases
(github-{slug}), and GitHub key labels, so the output charset is strictly
[a-z0-9-]. Uniqueness per user (appending -2, -3, ...) is handled by the
project service inside its transaction, not here.
"""

import re

_MAX_SLUG_LENGTH = 40


def slugify(name: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with single hyphens,
    strip leading/trailing hyphens, cap at 40 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug[:_MAX_SLUG_LENGTH].strip("-")
    return slug or "project"
