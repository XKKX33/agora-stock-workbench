from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from engine.security import redact_for_client, redact_secrets

logger = logging.getLogger(__name__)


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
    """生成可持久化的错误文案，删除凭据并限制异常正文长度。

    落库/日志用:保留路径,本地排障需要它定位文件。
    """
    return redact_secrets(str(error), limit=1000)


def client_error_message(error: BaseException) -> str:
    """生成发给浏览器的错误文案:凭据与服务器绝对路径都去掉。"""
    return redact_for_client(str(error), limit=1000)


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
                    "message": redact_for_client(error.message),
                    "details": error.details,
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(
        _request: Request, error: HTTPException
    ) -> JSONResponse:
        """把 FastAPI 自带的 HTTPException 也套进统一信封。

        不套的话响应是 {"detail": "Not Found"},而前端 api.js 只认
        payload.error.message,拿不到就一律显示"请求失败",真实原因丢失。
        """
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": f"http_{error.status_code}",
                    "message": redact_for_client(str(error.detail)),
                    "details": {},
                }
            },
            headers=getattr(error, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_uncaught_exception(
        request: Request, error: Exception
    ) -> JSONResponse:
        """未捕获异常。默认响应是 text/plain 的 "Internal Server Error",
        既不符合本文件声明的信封,也让前端无法显示任何原因。

        栈只进服务端日志;给调用方的正文经过凭据与路径双重脱敏。
        """
        logger.exception(
            "未捕获异常: %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": client_error_message(error),
                    "details": {"type": type(error).__name__},
                }
            },
        )
