"""FastAPI application assembly for Douyin Recall."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.db import init_schema
import src.web.routes.auth as auth_routes
import src.web.routes.content as content
import src.web.routes.jobs as jobs_routes
import src.web.routes.maintenance as maintenance_routes
import src.web.routes.media as media_routes
import src.web.routes.setup as setup_routes
from src.web.middleware import attach_current_user
from src.web import runtime
from src.web.security import validate_web_security_config


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_web_security_config()
    await asyncio.to_thread(init_schema)
    runtime.start_background_workers()
    try:
        yield
    finally:
        await asyncio.to_thread(runtime.shutdown_workers)


app = FastAPI(title="douyin-recall", lifespan=lifespan)
app.middleware("http")(attach_current_user)

app.include_router(auth_routes.router)
app.include_router(setup_routes.router)
app.include_router(jobs_routes.router)
app.include_router(maintenance_routes.router)
app.include_router(media_routes.router)
app.include_router(content.router)
