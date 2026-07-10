"""Per-project deploy keypair generation.

Same pattern as app_key_service, but these keys are scoped to one GitHub repo
each: one project = one key on GitHub = one private key file on the VPS.
"""

import asyncssh


def generate_deploy_keypair() -> tuple[bytes, str, str]:
    """Generate an ed25519 keypair for a project deploy key.

    Returns (private_key_openssh_bytes, public_key_openssh_text,
    fingerprint_sha256).
    """
    private_key = asyncssh.generate_private_key(
        "ssh-ed25519", comment="abstract-project-deploy"
    )
    public_openssh = private_key.export_public_key().decode("utf-8").strip()
    private_openssh = private_key.export_private_key()
    fingerprint = private_key.get_fingerprint()
    return private_openssh, public_openssh, fingerprint
