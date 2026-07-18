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
- **Install nginx**: installs nginx and its Let's Encrypt integration
  (`python3-certbot-nginx`). Nginx runs as a system service and will be used to
  route HTTPS traffic to projects in a future feature.
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
nginx, sudo user, firewall, swap, disable password authentication, disable root
login).
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
partial clone), so a retry starts clean. Deleting a project reverses all of this
plus everything accumulated since; see [Deleting a project](#deleting-a-project).

## Environment variables

Each project can hold any number of env files (for example `.env` at the repo
root, or `backend/.env` and `frontend/.env` in a monorepo). Paths are relative to
the clone directory and validated so they can never escape it.

Values are secrets. They are encrypted at rest (Fernet via the KeyProvider, same
scheme as SSH keys) the moment they arrive, and the API never returns them again:
list responses carry variable counts, detail responses carry key names only. In
the edit dialog, saved values show a hidden placeholder; leave a field untouched
to keep the stored value, type to replace it.

Conveniences:

- **Paste from .env**: paste the contents of an existing dotenv file and Abstract
  parses it into the editor (comments and `export` prefixes handled, quotes
  stripped, no `${VAR}` interpolation).
- **Write-both behavior**: on start, each env file is written to its configured
  path on the VPS AND all variables are merged into a root `.env` (unless you
  manage a root `.env` yourself), so `${VAR}` substitution in docker-compose.yml
  works automatically. A variable defined with different values in two files is
  rejected before anything is written.

## Running your app

Abstract is bring-your-own-container: it does not build images or guess your
stack. Clicking **Start** on a project card runs
`docker compose up -d --build` in the clone directory over the pooled SSH
connection, then verifies every service reports `running` (collecting the last
50 log lines of anything that does not).

Compose file discovery order: `compose.yaml`, `compose.yml`,
`docker-compose.yaml`, `docker-compose.yml`. If your compose file has a
non-standard name like `docker-compose.prod.yml`, set it in the project's
settings (gear icon); the setting is a path relative to the clone directory.

Env files are written to the VPS (mode 600, over SFTP) right before compose
up. If your repo has files committed at those same paths, they are overwritten
and a warning is logged. Compose up gets a generous 15 minute budget because
first builds on small VPSes are slow. On failure the card flips to "Failed to
start" and the full build transcript is shown in the UI (large logs are
truncated to the last 200KB, where the errors are); retries are safe.

Abstract treats your current docker-compose.yml as the source of truth. If you
remove a service from your compose file, its containers are cleaned up
automatically the next time you Start the project, and verification only checks
the services your file still defines. Containers from unrelated projects on the
VPS are never touched.

## Publishing your app

Publishing points a domain at a running app: Abstract writes an nginx server
block (with websocket upgrade headers included by default), enables it, obtains
a Let's Encrypt certificate with certbot, and verifies the app answers over
HTTPS. Certificate renewal is automatic forever via certbot's systemd timer;
there is nothing to maintain.

Preconditions:

- The app is running (Start it first; the Publish button is disabled otherwise).
- nginx is installed on the server (the "Install nginx" hardening operation).
- Your DNS A record for the domain points at the server's IP. Abstract checks
  this before touching the VPS and tells you what the domain currently resolves
  to if it does not match.

The internal port is detected automatically from the running containers'
published ports. Ports that look like databases (5432, 3306, 27017, 6379,
11211, 9200) are hidden by default so a database is not published to the
internet by accident; manual entry is available if you know what you are doing.
One domain and one internal port per server per project, enforced at the
database level.

If any step fails (nginx rejects the config, certbot cannot complete the
challenge, nothing answers on the port), Abstract cleans up what it created
(config, symlink, certificate), reloads nginx, and reports the captured output.
The database state changes only when the whole flow succeeds.

## Deleting a project

Deletion is the exact reverse of creation plus teardown of everything a project
accumulates afterward. It removes the container stack, the clone, the published
nginx config and Let's Encrypt certificate, the ssh config block and deploy key
files on the VPS, the GitHub deploy key, and finally the database rows (env files
and variables cascade). Once a VPS is registered on Abstract the only path to it
is through Abstract, so the clone directory is always removed; there is no opt-out.
The delete dialog requires typing the project name to confirm.

The steps run in a fixed order, each one idempotent so a retry after a partial
failure lands cleanly:

1. **unpublish** (skipped if never published): remove the nginx symlink and
   config, delete the certificate (guarded so a retry after a successful delete is
   safe), then validate and reload nginx.
2. **stop containers** (skipped if not running): `docker compose down
   --remove-orphans` in the clone, honoring a compose file override. A missing
   compose file is a no-op rather than an error.
3. **delete clone**: `rm -rf` the clone directory, under sudo so root-owned build
   artifacts a container may have written are removed too.
4. **remove ssh config block** and **delete deploy key files** on the VPS.
5. **revoke the GitHub deploy key** (a 404 counts as already gone).
6. **delete the database row**, cascading to env files and variables.

While a delete is in flight the project carries an `is_deleting` flag: it is set
in its own commit before any external teardown runs, so concurrent mutations are
rejected with 409, and the card shows a "deletion in progress" banner. If any step
fails the whole deletion aborts with no orphan records anywhere: the row stays,
the flag is cleared so you can retry, and the API returns a 502 whose body names
the failed step and the full per-step progress. On success the row is
hard-deleted; there is no soft-delete.

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

### One app key per server

Each server gets its own freshly generated ed25519 keypair, created during probe and
tied to the server row by a unique `server_id` (deleting the server cascades the key).
This limits blast radius: a compromised key only exposes the single server it was
installed on, never the user's other servers. The public key carries an
`abstract-server-<id8>` comment so a user can trace a line in their VPS
`authorized_keys` back to a specific Abstract server.

To open a key based connection Abstract needs the decrypted app private key.
Decrypting on every request is wasteful, so after the first decrypt the plaintext key
is cached in Redis under `ssh_key:{server_id}:{session_id}` with a TTL, where
`server_id` scopes the cache to the one server that key belongs to and `session_id`
is the Clerk session id so it is also scoped per login. Because that plaintext is
sensitive, the dev Redis container runs with `--appendonly no --save ""` and no
volume: nothing is ever written to disk and the cache does not survive a restart.

### The `_from_client` convention

Any value that originates from a request (body, path, query, header) is named with a
`_from_client` suffix at the point it enters Python. This is a code review aid: it
makes client controlled values visually obvious so they are never used where a
server controlled value is required.

### user_id is never from the client

User identity always comes from the authenticated session (`current_user.id`), never
from request input. Every database filter and Redis key is scoped by a server side id
(the user id, or a server id first resolved through an owned server), never by a
client supplied value. Identity enters the system only through the Clerk session token,
verified by the backend; the `sub` (Clerk user id) and `sid` (session id) claims are
read only after signature verification, so they are trusted credentials rather than
client input.

### Ownership helper

`get_owned_server` resolves a server by id and confirms it belongs to the current
user, returning 404 (not 403) on a mismatch so the API does not leak which server
ids exist.
