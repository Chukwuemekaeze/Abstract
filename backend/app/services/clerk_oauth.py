"""GitHub OAuth token retrieval via Clerk.

Clerk holds the user's GitHub OAuth access token (repo scope, granted at
sign-in). We fetch it fresh on every operation that needs it and never cache
it in Redis or Postgres: Clerk owns the token lifecycle.
"""

from clerk_backend_api import Clerk
from clerk_backend_api.models import ClerkErrors, SDKError


class GithubTokenUnavailable(Exception):
    """The user has no linked GitHub account or Clerk returned no token."""


async def get_github_oauth_token(clerk: Clerk, clerk_user_id: str) -> str:
    """Fetch the user's GitHub OAuth access token from Clerk.

    Raises GithubTokenUnavailable if the user has no linked GitHub account or
    Clerk returns no usable token. Other Clerk transport errors propagate.
    """
    try:
        tokens = await clerk.users.get_o_auth_access_token_async(
            user_id=clerk_user_id, provider="oauth_github"
        )
    except (ClerkErrors, SDKError) as exc:
        # Clerk answers 404/400 when the user has no oauth_github external
        # account. Any of these means the same thing to the caller: no token.
        raise GithubTokenUnavailable(str(exc)) from exc

    if not tokens or not tokens[0].token:
        raise GithubTokenUnavailable(
            f"Clerk returned no GitHub OAuth token for user {clerk_user_id}"
        )
    return tokens[0].token
