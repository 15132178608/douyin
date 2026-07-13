"""FastAPI application assembly for Douyin Recall."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os

from fastapi import FastAPI

from src import server_runtime
from src.config import settings
from src.db import init_schema
import src.web.routes.auth as auth_routes
import src.web.routes.browse as browse_routes
import src.web.routes.categories as categories_routes
import src.web.routes.item_actions as item_action_routes
import src.web.routes.jobs as jobs_routes
import src.web.routes.maintenance as maintenance_routes
import src.web.routes.media as media_routes
import src.web.routes.setup as setup_routes
from src.web.middleware import attach_current_user
from src.web import runtime
from src.web.security import validate_web_security_config


async def _run_sync_before_unlock(sync_func) -> None:
    """Delay cancellation until protected synchronous database work is finished."""
    sync_task = asyncio.create_task(asyncio.to_thread(sync_func))
    cancellation: asyncio.CancelledError | None = None
    while not sync_task.done():
        try:
            await asyncio.shield(sync_task)
        except asyncio.CancelledError as exc:
            cancellation = exc
    sync_task.result()
    if cancellation is not None:
        raise cancellation


@asynccontextmanager
async def lifespan(app: FastAPI):
    with server_runtime.database_runtime_lock():
        validate_web_security_config()
        await _run_sync_before_unlock(init_schema)
        runtime.start_background_workers(reset_shutdown=True)
        state = None
        try:
            state = server_runtime.write_server_state(
                pid=os.getpid(),
                host=settings.web_host,
                port=settings.web_port,
            )
            yield
        finally:
            try:
                await _run_sync_before_unlock(runtime.shutdown_workers)
            finally:
                if state is not None:
                    current = server_runtime.read_server_state()
                    if current is not None and current.pid == state.pid:
                        server_runtime.clear_server_state()


app = FastAPI(title="douyin-recall", lifespan=lifespan)
app.middleware("http")(attach_current_user)

app.include_router(auth_routes.router)
app.include_router(setup_routes.router)
app.include_router(jobs_routes.router)
app.include_router(maintenance_routes.router)
app.include_router(media_routes.router)
app.include_router(browse_routes.router)
app.include_router(categories_routes.router)
app.include_router(item_action_routes.router)
