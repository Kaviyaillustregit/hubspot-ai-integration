import logging
from typing import cast

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str | None


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _request_id(request: Request) -> str | None:
    return cast(str | None, getattr(request.state, "request_id", None))


def _response(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            code=code, message=message, request_id=_request_id(request)
        ).model_dump(),
    )


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error = cast(AppError, exc)
    return _response(request, error.code, error.message, error.status_code)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return _response(request, "validation_error", "Request validation failed", 422)


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    error = cast(StarletteHTTPException, exc)
    return _response(request, "http_error", str(error.detail), error.status_code)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled application error")
    return _response(request, "internal_error", "An internal error occurred", 500)