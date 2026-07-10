"""GitHub REST API client for deploy key management and repo listing.

All calls authenticate with the user's OAuth token (fetched fresh from Clerk,
never cached by us). Deploy keys are registered read-only and scoped to a
single repo, which is the whole point of the per-project key model.
"""

from datetime import datetime, timezone

import httpx

from app.schemas.projects import GithubRepoResponse

_BASE_URL = "https://api.github.com"
_TIMEOUT = 30.0
# Sanity cap on pagination so a user with thousands of repos cannot make one
# dropdown request fan out into dozens of GitHub calls.
_MAX_REPOS = 500


class GithubApiError(Exception):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"GitHub API returned {status_code}")


class GithubRateLimited(Exception):
    def __init__(self, reset_at: datetime):
        self.reset_at = reset_at
        super().__init__(f"GitHub rate limit exceeded; resets at {reset_at.isoformat()}")


class GithubRepoNotFound(Exception):
    """Repo is gone or the token lacks admin on it."""


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _check_rate_limit(response: httpx.Response) -> None:
    if (
        response.status_code == 403
        and response.headers.get("X-RateLimit-Remaining") == "0"
    ):
        reset_raw = response.headers.get("X-RateLimit-Reset", "0")
        try:
            reset_at = datetime.fromtimestamp(int(reset_raw), tz=timezone.utc)
        except ValueError:
            reset_at = datetime.now(timezone.utc)
        raise GithubRateLimited(reset_at)


class GithubService:
    async def list_admin_repos(self, token: str) -> list[GithubRepoResponse]:
        """Repos where the user has admin permission, newest push first.

        GitHub sorts by pushed_at when sort=pushed; we keep that order and only
        filter out repos the user cannot add deploy keys to (admin required).
        """
        repos: list[GithubRepoResponse] = []
        url: str | None = (
            f"{_BASE_URL}/user/repos"
            "?sort=pushed&direction=desc&per_page=100"
            "&affiliation=owner,collaborator,organization_member"
        )
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while url is not None and len(repos) < _MAX_REPOS:
                response = await client.get(url, headers=_headers(token))
                _check_rate_limit(response)
                if response.status_code != 200:
                    raise GithubApiError(response.status_code, response.text)
                for repo in response.json():
                    if len(repos) >= _MAX_REPOS:
                        break
                    permissions = repo.get("permissions") or {}
                    if permissions.get("admin") is not True:
                        continue
                    repos.append(
                        GithubRepoResponse(
                            id=repo["id"],
                            full_name=repo["full_name"],
                            name=repo["name"],
                            pushed_at=repo.get("pushed_at"),
                            private=repo["private"],
                        )
                    )
                url = response.links.get("next", {}).get("url")
        return repos

    async def add_deploy_key(
        self,
        token: str,
        repo_full_name: str,
        title: str,
        public_key: str,
        read_only: bool = True,
    ) -> int:
        """Register a deploy key on the repo. Returns GitHub's numeric key ID."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{_BASE_URL}/repos/{repo_full_name}/keys",
                headers=_headers(token),
                json={"title": title, "key": public_key, "read_only": read_only},
            )
        _check_rate_limit(response)
        if response.status_code == 404:
            raise GithubRepoNotFound(repo_full_name)
        if response.status_code not in (200, 201):
            raise GithubApiError(response.status_code, response.text)
        return response.json()["id"]

    async def get_ssh_host_keys(self, token: str) -> list[str]:
        """GitHub's published SSH host keys, e.g. 'ssh-ed25519 AAAA...'.

        Fetched from the /meta endpoint over TLS, so trust is anchored in the
        certificate chain rather than in whatever a network path answers to an
        ssh-keyscan. Used to seed known_hosts on the VPS before the first clone.
        """
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"{_BASE_URL}/meta", headers=_headers(token)
            )
        _check_rate_limit(response)
        if response.status_code != 200:
            raise GithubApiError(response.status_code, response.text)
        return response.json().get("ssh_keys") or []

    async def delete_deploy_key(
        self, token: str, repo_full_name: str, github_deploy_key_id: int
    ) -> None:
        """Delete a deploy key. 404 means already gone, which is success."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.delete(
                f"{_BASE_URL}/repos/{repo_full_name}/keys/{github_deploy_key_id}",
                headers=_headers(token),
            )
        _check_rate_limit(response)
        if response.status_code in (204, 404):
            return
        raise GithubApiError(response.status_code, response.text)


github_service = GithubService()
