# coding: utf-8
"""Project business errors.

FastAPI maps these to the same JSON shape as HTTPException: ``{"detail": "..."}``.
Routers should raise AppError instead of HTTPException; status codes stay explicit.
"""
from __future__ import annotations


class AppError(Exception):
    """Business error with an HTTP status code and public detail string."""

    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        self.detail = detail
        self.status_code = int(status_code)
        super().__init__(detail)


class ValidationAppError(AppError):
    """400-level input error. Detail text must remain API-stable."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, status_code=400)
