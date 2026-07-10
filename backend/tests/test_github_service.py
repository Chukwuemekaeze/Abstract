"""GitHub API client tests with httpx mocked out.

No network: httpx.AsyncClient is replaced with a fake that replays canned
httpx.Response objects, which keeps real Link header parsing and status
handling in play.
"""

from datetime import datetime, timezone

import httpx
import pytest

from app.services.github_service import (
    GithubApiError,
    GithubRateLimited,
    GithubRepoNotFound,
    GithubService,
)

TOKEN = "gho_test_token"


class FakeAsyncClient:
    """Replays queued responses; records every request made."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def _next(self, method, url):
        self.requests.append((method, url))
        return self._responses.pop(0)

    async def get(self, url, headers=None):
        return self._next("GET", url)

    async def post(self, url, headers=None, json=None):
        return self._next("POST", url)

    async def delete(self, url, headers=None):
        return self._next("DELETE", url)


def _response(status, json_body=None, headers=None, url="https://api.github.com/x"):
    return httpx.Response(
        status,
        json=json_body,
        headers=headers or {},
        request=httpx.Request("GET", url),
    )


@pytest.fixture
def github_with(mocker):
    """Returns (service, install) where install(responses) wires the fake client."""
    service = GithubService()

    def install(responses):
        fake = FakeAsyncClient(responses)
        mocker.patch(
            "app.services.github_service.httpx.AsyncClient",
            side_effect=lambda **kwargs: fake,
        )
        return fake

    return service, install


def _repo(repo_id, full_name, admin, pushed_at="2026-07-01T12:00:00Z"):
    return {
        "id": repo_id,
        "full_name": full_name,
        "name": full_name.split("/")[1],
        "pushed_at": pushed_at,
        "private": True,
        "permissions": {"admin": admin, "push": True, "pull": True},
    }


async def test_list_admin_repos_filters_non_admin(github_with):
    service, install = github_with
    install(
        [
            _response(
                200,
                [
                    _repo(1, "me/admin-repo", admin=True),
                    _repo(2, "org/read-only-repo", admin=False),
                ],
            )
        ]
    )
    repos = await service.list_admin_repos(TOKEN)
    assert [r.id for r in repos] == [1]
    assert repos[0].full_name == "me/admin-repo"


async def test_list_admin_repos_paginates_via_link_header(github_with):
    service, install = github_with
    page_two_url = "https://api.github.com/user/repos?page=2"
    fake = install(
        [
            _response(
                200,
                [_repo(1, "me/one", admin=True)],
                headers={"Link": f'<{page_two_url}>; rel="next"'},
            ),
            _response(200, [_repo(2, "me/two", admin=True)]),
        ]
    )
    repos = await service.list_admin_repos(TOKEN)
    assert [r.id for r in repos] == [1, 2]
    assert fake.requests[1] == ("GET", page_two_url)


async def test_add_deploy_key_returns_numeric_id(github_with):
    service, install = github_with
    fake = install([_response(201, {"id": 987654, "key": "ssh-ed25519 AAAA"})])
    key_id = await service.add_deploy_key(
        TOKEN, "me/repo", "Abstract: Test", "ssh-ed25519 AAAA", read_only=True
    )
    assert key_id == 987654
    assert fake.requests == [
        ("POST", "https://api.github.com/repos/me/repo/keys")
    ]


async def test_add_deploy_key_404_raises_repo_not_found(github_with):
    service, install = github_with
    install([_response(404, {"message": "Not Found"})])
    with pytest.raises(GithubRepoNotFound):
        await service.add_deploy_key(TOKEN, "me/gone", "t", "ssh-ed25519 AAAA")


async def test_add_deploy_key_error_carries_status_and_body(github_with):
    service, install = github_with
    install([_response(422, {"message": "key is already in use"})])
    with pytest.raises(GithubApiError) as exc_info:
        await service.add_deploy_key(TOKEN, "me/repo", "t", "ssh-ed25519 AAAA")
    assert exc_info.value.status_code == 422
    assert "already in use" in exc_info.value.body


async def test_delete_deploy_key_treats_404_as_success(github_with):
    service, install = github_with
    install([_response(404, {"message": "Not Found"})])
    await service.delete_deploy_key(TOKEN, "me/repo", 987654)


async def test_rate_limit_raises_with_parsed_reset(github_with):
    service, install = github_with
    reset = 1783000000
    install(
        [
            _response(
                403,
                {"message": "API rate limit exceeded"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
            )
        ]
    )
    with pytest.raises(GithubRateLimited) as exc_info:
        await service.list_admin_repos(TOKEN)
    assert exc_info.value.reset_at == datetime.fromtimestamp(reset, tz=timezone.utc)
