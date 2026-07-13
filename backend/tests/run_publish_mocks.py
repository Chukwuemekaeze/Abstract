"""Shared fakes and seeders for env file, run, and publish tests.

Builds on project_mocks: the same substring-scripted connection fake, plus
builders for `docker compose ps --format json` output in both shapes (NDJSON
from newer compose, JSON array from older) and DB seeders for a hardened
server with a cloned project.
"""

import json
from datetime import datetime, timezone

from app.models import Project, ProjectDeployKey, Server

CLONE_PATH = "/home/deploy/anibantsdotNG"


def service_entry(
    name: str = "web",
    state: str = "running",
    publishers: list[dict] | None = None,
) -> dict:
    return {
        "Service": name,
        "Name": f"app-{name}-1",
        "State": state,
        "Publishers": publishers or [],
    }


def publisher(host_port: int, container_port: int, url: str = "0.0.0.0") -> dict:
    return {
        "URL": url,
        "TargetPort": container_port,
        "PublishedPort": host_port,
        "Protocol": "tcp",
    }


def ps_ndjson(services: list[dict]) -> str:
    return "\n".join(json.dumps(s) for s in services) + "\n"


def ps_array(services: list[dict]) -> str:
    return json.dumps(services)


async def make_server(
    db_session,
    user_id,
    *,
    host="203.0.113.10",
    sudo_user_name="deploy",
    nginx_installed=True,
    docker_installed=True,
    name="web1",
):
    server = Server(
        user_id=user_id,
        name=name,
        host=host,
        port=22,
        username=sudo_user_name or "root",
        status="verified",
        verification_source="tofu",
        sudo_user_name=sudo_user_name,
        base_packages_installed=True,
        docker_installed=docker_installed,
        nginx_installed=nginx_installed,
    )
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)
    return server


async def seed_project(
    db_session,
    user_id,
    server,
    *,
    slug="seeded",
    repo_id=777001,
    runtime_status="never_started",
    domain=None,
    internal_port=None,
    published_at=None,
    compose_file_path=None,
):
    project = Project(
        user_id=user_id,
        server_id=server.id,
        name=slug,
        slug=slug,
        github_repo_full_name="Chukwuemekaeze/anibantsdotNG",
        github_repo_id=repo_id,
        clone_path=CLONE_PATH,
        cloned_at=datetime.now(timezone.utc),
        runtime_status=runtime_status,
        domain=domain,
        internal_port=internal_port,
        published_at=published_at,
        compose_file_path=compose_file_path,
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        ProjectDeployKey(
            project_id=project.id,
            github_deploy_key_id=111,
            deploy_key_public_key="ssh-ed25519 AAAA seeded",
            encrypted_deploy_key_private_key=b"ciphertext",
            deploy_key_fingerprint="SHA256:seededfingerprint",
        )
    )
    await db_session.commit()
    await db_session.refresh(project)
    return project
