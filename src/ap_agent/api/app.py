"""FastAPI application factory for the Phase 9 human-review interface.

Source: notebook cell 108's `phase_9_api = FastAPI(...)` and its route
definitions, restructured into `ap_agent.api.routes.*` (task §3) behind an
application factory (task §8: `create_app(...)`), with:

- no live database connection opened at import time or at factory-call
  time -- `create_app` only stores the DSN/config on `app.state`;
  `ap_agent.api.dependencies.get_review_repository` builds a fresh
  `ReviewRepository` per request from that state (task §8: "Avoid
  constructing live database connections during module import.");
- FastAPI lifespan management for the one resource this API owns whose
  lifetime should be tied to the app's (task §8/§13): a
  `psycopg_pool.ConnectionPool`, opened non-blocking at startup and closed
  at shutdown purely as an early-warning readiness signal and for a future
  pooled repository -- `ReviewRepository` itself still opens one
  short-lived connection per call, exactly like
  `ap_agent.repositories.postgres_memory_repository.PostgresMemoryRepository`
  (M8's own precedent); this is a documented simplification, not a silent
  one (see `docs/m10_phase_9_review_api_report.md`);
- configurable CORS with no permissive wildcard default (task §13):
  `ApiConfig.cors_allow_origins` defaults to empty (no CORS middleware
  installed at all), and rejects `"*"` if ever set (`ApiConfig.__post_init__`);
- `ap_agent.api.errors.register_exception_handlers` installed once, so
  every route's failure path funnels through the same deterministic error
  envelope (task §12).

No PaddleOCR/heavyweight OCR module is imported anywhere in this package
or its dependents (`ap_agent.repositories.review_repository`,
`ap_agent.services.review_*`) -- this app starts without downloading any
OCR model (task §13).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ap_agent.api.config import ApiConfig, load_api_config
from ap_agent.api.errors import register_exception_handlers
from ap_agent.api.routes import dashboard, health, review_cases, review_commands
from ap_agent.config.postgres import MemoryConfig
from ap_agent.models.interface import InterfaceConfig, default_interface_config

__all__ = ["create_app"]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = None

    try:
        from ap_agent.db.connection import create_connection_pool

        pool = create_connection_pool(app.state.postgres_dsn, app.state.memory_config)
        pool.open(wait=False)
    except Exception:  # pragma: no cover - readiness signal only, never blocks startup
        pool = None

    app.state.connection_pool = pool

    try:
        yield
    finally:
        if pool is not None:
            pool.close()


def create_app(
    *,
    dsn: Optional[str] = None,
    memory_config: Optional[MemoryConfig] = None,
    interface_config: Optional[InterfaceConfig] = None,
    api_config: Optional[ApiConfig] = None,
) -> FastAPI:
    """Build the Phase 9 review API. Every parameter is optional so the
    `uvicorn ap_agent.api.app:create_app --factory` launch command (task
    §8) works with zero arguments, reading configuration from the
    environment at call time -- never at import time. Tests instead call
    this directly with an explicit `dsn`/`memory_config` (e.g. the isolated
    `ap_agent_m8_test` database), never through the factory string.
    """

    resolved_memory_config = memory_config or MemoryConfig()

    if dsn is None:
        from ap_agent.db.connection import load_dsn

        dsn = load_dsn(resolved_memory_config)

    resolved_api_config = api_config or load_api_config()
    resolved_interface_config = interface_config or default_interface_config()

    app = FastAPI(
        title="Accounts Payable Agent Review API",
        description=(
            "Tenant-isolated human-review API for the accounts payable agent. "
            "Payment execution is not supported."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )

    app.state.postgres_dsn = dsn
    app.state.memory_config = resolved_memory_config
    app.state.api_config = resolved_api_config
    app.state.interface_config = resolved_interface_config

    if resolved_api_config.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_api_config.cors_allow_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(dashboard.router)
    app.include_router(review_cases.router)
    app.include_router(review_commands.router)

    return app
