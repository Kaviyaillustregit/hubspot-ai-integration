from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.errors import (
    AppError,
    app_error_handler,
    http_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.api.health import router as health_router
from app.api.hubspot_contacts import router as hubspot_contacts_router
from app.api.hubspot_oauth import router as hubspot_oauth_router
from app.api.middleware import RequestIdMiddleware
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import create_database


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    db_engine, session_factory = create_database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.session_factory = session_factory
        app.state.db_engine = db_engine
        yield
        await db_engine.dispose()

    application = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    application.add_exception_handler(AppError, app_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(StarletteHTTPException, http_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)
    application.add_middleware(RequestIdMiddleware)
    application.include_router(health_router, prefix="/api/v1")
    application.include_router(hubspot_oauth_router, prefix="/api/v1")
    application.include_router(hubspot_contacts_router, prefix="/api/v1")
    return application
