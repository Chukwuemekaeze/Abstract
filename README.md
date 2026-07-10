# Abstract (v1)

Abstract is an open source deployment pipeline. Developers register a VPS, verify its
SSH host key fingerprint using a trust on first use (TOFU) flow, let Abstract install
its own public key for ongoing authentication, and trigger actions from a UI. This
first milestone covers server registration, host key verification, public key
installation, and a minimal "deploy hello world" smoke test that proves the SSH
connection pool works end to end. Actual deployment logic, log tailing, and
container management are future milestones.

## Authentication

Abstract uses [Clerk](https://clerk.com) for authentication, with GitHub OAuth as the
sign in method. To run the app locally:

1. Create a Clerk application in the Clerk dashboard.
2. Enable GitHub as a social connection and request the `repo` scope.
3. Copy the publishable key and secret key from the dashboard into the env files
   (see Backend setup and Frontend setup below), and set `CLERK_JWT_ISSUER` to your
   Clerk instance URL (the Frontend API / Issuer value in the dashboard).

The frontend gates every page behind Clerk: unauthenticated visitors are redirected
to `/sign-in`. The backend verifies the Clerk session token on every request. The
first time a user signs in, Abstract lazily creates a matching row in the Postgres
`users` table (keyed by the Clerk user id, with the email pulled from Clerk). There
is no separate backend signup flow; Clerk owns the entire account lifecycle.

## Architecture at a glance

- Backend: async FastAPI, async SQLAlchemy 2.0 (asyncpg), async Redis, asyncssh.
- Frontend: React 19, Vite, Tailwind v4, shadcn/ui, TanStack Query, Zustand.
- Database: Postgres (hosted on Neon in production; a local Postgres is provided in
  the dev compose for convenience).
- Cache: Redis, with persistence fully disabled (see Architecture notes).

## Prerequisites

- Node 20+
- Python 3.12
- Docker (for the dev Redis and the local dev Postgres)

## Backend setup

All commands run from the `backend/` directory unless noted.

1. Create the virtual environment and install dependencies (uses uv):

   ```bash
   uv venv .venv --python 3.12
   uv pip install --python .venv/bin/python -e ".[dev]"
   ```

   (Plain pip works too: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`.)

2. Copy the env template and fill it in:

   ```bash
   cp .env.example .env
   ```

   - `DATABASE_URL`: your Neon connection string (must use the `postgresql+asyncpg://`
     driver prefix). For purely local work you can point it at the dev Postgres:
     `postgresql+asyncpg://deploy:deploy@localhost:5432/deploy_pipeline`.
   - `APP_MASTER_KEY`: generate one with:

     ```bash
     python -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
     ```

   - `CLERK_SECRET_KEY`, `CLERK_PUBLISHABLE_KEY`, `CLERK_JWT_ISSUER`, and
     `CLERK_AUTHORIZED_PARTIES`: from your Clerk dashboard (see Authentication above).
     `CLERK_AUTHORIZED_PARTIES` is a comma separated list of allowed frontend origins
     (for local dev, `http://localhost:5173`).

3. Start the dev services (Redis, and the local dev Postgres) from the repo root:

   ```bash
   docker compose -f docker-compose.dev.yml up -d
   ```

   If you already run Redis on port 6379, either stop it or use only Postgres with
   `docker compose -f docker-compose.dev.yml up -d postgres` and point `REDIS_URL`
   at your existing Redis.

4. Run migrations:

   ```bash
   .venv/bin/alembic upgrade head
   ```

   There is no user to seed. User rows are created lazily on first sign in via Clerk.

5. Start the API with autoreload:

   ```bash
   .venv/bin/uvicorn app.main:app --reload --port 8000
   ```

   Interactive docs are at http://localhost:8000/docs.

### Running the backend tests

The suite creates its own test database on demand, so just point it at a Postgres
server with `TEST_DATABASE_URL` and run pytest:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://deploy:deploy@localhost:5432/deploy_pipeline_test \
  .venv/bin/python -m pytest
```

Tests that do not need a database (key provider, mocked SSH service) run even
without `TEST_DATABASE_URL` set; the DB backed ones skip in that case.

## Frontend setup

From the `frontend/` directory:

```bash
npm install
cp .env.example .env.local   # then set VITE_CLERK_PUBLISHABLE_KEY
npm run dev
```

`.env.local` (gitignored) holds `VITE_CLERK_PUBLISHABLE_KEY`, your Clerk publishable
key. The app throws on startup if it is missing.

The dev server runs on http://localhost:5173 and proxies `/api` to the backend on
port 8000, so there is no CORS configuration to worry about during development.

## Smoke test (end to end)

1. Start the backend (port 8000) and the frontend (port 5173).
2. Open http://localhost:5173. You will be redirected to `/sign-in`. Click
   "Continue with GitHub" and complete the OAuth flow. After signing in you land on
   the servers page. Click "Add server".
3. Enter a name, your VPS IP, port (22), username (root), and the root password.
4. Click Continue. Abstract probes the host and shows its SHA256 fingerprint.
5. Compare that fingerprint against the one in your VPS provider's console. If they
   match, click "Fingerprint matches, install key".
6. Abstract installs its public key over the password session, optionally disables
   password authentication, and verifies key based login works.
7. On the verified server card, click "Run smoke test". You should see the output of
   `echo 'hello from Abstract' && uname -a && date`, proving the pooled
   key based SSH connection works end to end.

## Hardening

Once a server is verified you can make it deployment-ready from its detail page
(click a server name in the list to open `/servers/:id`). Each operation is a card
with a Run button and, on failure, a collapsible panel showing the captured shell
output.

Available operations:

- **Update system**: `apt-get update` and `upgrade` (noninteractive).
- **Install base packages**: git, certbot, ufw, curl, ca-certificates.
- **Install Docker**: the official `get.docker.com` installer.
- **Create sudo user**: a non-root user with passwordless sudo and the app deploy
  key. This is the point where Abstract switches from operating as root to operating
  as the sudo user.
- **Disable root login**: sets `PermitRootLogin no` and reloads sshd. Disabled until
  a sudo user exists, since Abstract refuses to remove root access without a
  confirmed alternative login.
- **Disable password authentication**: sets `PasswordAuthentication no` and reloads
  sshd. Safe because key based login is already verified. Useful when password auth
  was left enabled at install time.
- **Configure firewall**: allows OpenSSH, 80, and 443, then enables UFW.
- **Create swap**: a swap file sized at 25% of the box's RAM (with a 512MB floor).
- **Reboot**: reboots the box and polls until it comes back online.

**Quick harden** runs the standard sequence in order (update, base packages, Docker,
sudo user, firewall, swap, disable password authentication, disable root login).
Reboot is intentionally excluded so the connection is not dropped mid-sequence.

Quick harden and each individual operation are **atomic at the database level**: the
SSH commands and the resulting state writes run inside a single transaction that
commits only on success. If anything fails, the transaction rolls back and Abstract's
recorded state for the server is unchanged. The VPS itself may be partially changed
after a failure (we cannot undo `apt upgrade` or a Docker install), so every shell
command is written to be idempotent, which makes retries safe and correct.

## Projects

A project is a GitHub repository cloned onto a verified, hardened server, with a
per-project SSH deploy key managed by Abstract. Projects live at `/projects` (all
projects across servers) and in a Projects section on each server's detail page.

Preconditions: the server must be verified and hardened. Specifically it needs a
sudo user and the base packages (git) installed; the create flow checks both the
recorded state and the live box before doing anything.

User flow: click **New Project** on the `/projects` page or on a server detail
page, fill in a name, pick a server (preselected when opened from a server page),
pick a repo, and click Create. The repo picker lists the repos your GitHub account
has admin permission on, newest push first.

What Abstract does on create:

1. Generates a fresh ed25519 keypair for this project only.
2. Registers the public key as a **read-only deploy key** on the repo, using your
   GitHub OAuth token (via Clerk; the token is fetched fresh each time and never
   stored).
3. Writes the private key to `~/.ssh/{slug}-deploy` on the VPS (mode 600, sent
   over SFTP).
4. Adds a Host block to the sudo user's `~/.ssh/config` with a per-project alias
   (`github-{slug}`), so each clone uses its own key.
5. Clones the repo to `/home/{sudo_user_name}/{repo_name}` and verifies the clone.

Security model: every deploy key is scoped to exactly one project. One project =
one key on GitHub = one private key file on the VPS = one ssh config block. Keys
are read-only on the repo, stored encrypted (Fernet via the KeyProvider) in
Abstract's database, and as mode 600 files on the VPS. Compromise of one key
affects at most one repository.

Project creation follows the same atomicity discipline as hardening: all database
writes plus the external side effects run inside a single transaction that commits
only when the clone has been verified. On any failure the transaction rolls back,
and Abstract makes a best-effort attempt to undo the external state it created
(delete the GitHub deploy key, remove the key file and config block, remove a
partial clone), so a retry starts clean. Deleting projects is deferred to the
server-deletion milestone.

## Architecture notes

### Trust on first use (TOFU)

When you register a server Abstract opens an unauthenticated probe, captures the host
key the server presents, and stores it along with its SHA256 fingerprint. It does not
blindly trust it: the UI asks you to compare the fingerprint against your provider's
console before anything is installed. Once confirmed, every later connection uses
strict host key checking against the stored key, and a mismatch flips the server to
the `key_mismatch` status rather than connecting.

### KeyProvider abstraction

App SSH private keys are encrypted at rest. `KeyProvider` is a small interface
(`encrypt`, `decrypt`, `key_id`). v1 ships `EnvKeyProvider`, which uses Fernet keyed
from `APP_MASTER_KEY` (the documented key generator produces exactly a Fernet key).
A KMS backed provider can be added later behind the same interface without touching
call sites; `encryption_key_id` is stored on each key row so re-encryption is
possible.

### Redis cache lifecycle

To open a key based connection Abstract needs the decrypted app private key.
Decrypting on every request is wasteful, so after the first decrypt the plaintext key
is cached in Redis under `ssh_key:{user_id}:{session_id}` with a TTL, where
`session_id` is the Clerk session id so the cache is scoped per login. Because that
plaintext is sensitive, the dev Redis container runs with `--appendonly no --save ""`
and no volume: nothing is ever written to disk and the cache does not survive a
restart.

### The `_from_client` convention

Any value that originates from a request (body, path, query, header) is named with a
`_from_client` suffix at the point it enters Python. This is a code review aid: it
makes client controlled values visually obvious so they are never used where a
server controlled value is required.

### user_id is never from the client

User identity always comes from the authenticated session (`current_user.id`), never
from request input. Every database filter and every Redis key is scoped by that
server side id. Identity enters the system only through the Clerk session token,
verified by the backend; the `sub` (Clerk user id) and `sid` (session id) claims are
read only after signature verification, so they are trusted credentials rather than
client input.

### Ownership helper

`get_owned_server` resolves a server by id and confirms it belongs to the current
user, returning 404 (not 403) on a mismatch so the API does not leak which server
ids exist.
