# Abstract (v1)

Abstract is an open source deployment pipeline. Developers register a VPS, verify its
SSH host key fingerprint using a trust on first use (TOFU) flow, let Abstract install
its own public key for ongoing authentication, and trigger actions from a UI. This
first milestone covers server registration, host key verification, public key
installation, and a minimal "deploy hello world" smoke test that proves the SSH
connection pool works end to end. Actual deployment logic, log tailing, and
container management are future milestones.

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

3. Start the dev services (Redis, and the local dev Postgres) from the repo root:

   ```bash
   docker compose -f docker-compose.dev.yml up -d
   ```

   If you already run Redis on port 6379, either stop it or use only Postgres with
   `docker compose -f docker-compose.dev.yml up -d postgres` and point `REDIS_URL`
   at your existing Redis.

4. Run migrations and seed the stubbed dev user:

   ```bash
   .venv/bin/alembic upgrade head
   .venv/bin/python -m scripts.seed_dev_user
   ```

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
npm run dev
```

The dev server runs on http://localhost:5173 and proxies `/api` to the backend on
port 8000, so there is no CORS configuration to worry about during development.

## Smoke test (end to end)

1. Start the backend (port 8000) and the frontend (port 5173).
2. Open http://localhost:5173 and click "Add server".
3. Enter a name, your VPS IP, port (22), username (root), and the root password.
4. Click Continue. Abstract probes the host and shows its SHA256 fingerprint.
5. Compare that fingerprint against the one in your VPS provider's console. If they
   match, click "Fingerprint matches, install key".
6. Abstract installs its public key over the password session, optionally disables
   password authentication, and verifies key based login works.
7. On the verified server card, click "Run smoke test". You should see the output of
   `echo 'hello from deployment pipeline' && uname -a && date`, proving the pooled
   key based SSH connection works end to end.

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
is cached in Redis under `ssh_key:{user_id}:dev-session` with a TTL. Because that
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
server side id. In v1 authentication is stubbed to a seeded dev user, but the same
rule holds and real auth will slot in without changing call sites.

### Ownership helper

`get_owned_server` resolves a server by id and confirms it belongs to the current
user, returning 404 (not 403) on a mismatch so the API does not leak which server
ids exist.
