"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes import servers
from app.services.ssh_service import clear_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Close any pooled SSH connections on shutdown.
    clear_pool()


app = FastAPI(title="Deployment Pipeline", version="0.1.0", lifespan=lifespan)
app.include_router(servers.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}
