# coding: utf-8
"""Global FastAPI exception handlers.

Keep the public JSON contract identical to FastAPI's HTTPException:
``{"detail": "<message>"}``.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from common.errors import AppError


async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
