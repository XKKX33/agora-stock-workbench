from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from engine.security import redact_secrets


class WorkbenchError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def safe_error_message(error: BaseException) -> str:
    """生成可持久化的错误文案，删除凭据并限制异常正文长度。"""
    return redact_secrets(str(error), limit=1000)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = [
            {
                "location": list(item.get("loc", ())),
                "message": item.get("msg", "参数无效"),
                "type": item.get("type", "value_error"),
            }
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_failed",
                    "message": "请求参数校验失败",
                    "details": {"errors": details},
                }
            },
        )

    @app.exception_handler(WorkbenchError)
    async def handle_workbench_error(
        _request: Request, error: WorkbenchError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            },
        )
