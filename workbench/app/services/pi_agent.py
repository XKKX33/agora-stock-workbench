"""FastAPI 进程与本地 Pi Agent 服务的边界适配器。

Python 是协议和数据库的拥有者；这个模块只负责调用绑定在 loopback 上的 Node
服务。模型密钥只进入 Node 子进程环境变量，不经过 HTTP 请求体，也不会写入异常。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import httpx

from app.schemas.pi_agent import (
    PiAgentRequest,
    PiAgentValidationError,
    PiHealthResponse,
    PiJudgmentResult,
    PiRunResponse,
    validate_judgment_result,
)
from engine.security import redact_secrets

logger = logging.getLogger(__name__)


# 结果校验和 HTTP 边界共享同一公开异常类型，调用方无需区分两种非法协议。
PiAgentProtocolError = PiAgentValidationError


def _redacted_error(error: BaseException, secret: str | None = None) -> PiAgentProtocolError:
    message = redact_secrets(str(error), limit=1000)
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return PiAgentProtocolError(message or "Pi Agent 请求失败")
def _http_error(response: httpx.Response) -> PiAgentProtocolError:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("error") or payload.get("message") or "").strip()
    except ValueError:
        detail = response.text.strip()
    suffix = f": {redact_secrets(detail, limit=500)}" if detail else ""
    return PiAgentProtocolError(f"Pi Agent HTTP {response.status_code}{suffix}")




class PiAgentClient:
    """同步 HTTP 客户端，适合现有后台线程任务。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 180.0,
        stream_read_timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Pi Agent base_url 不能为空")
        if not token:
            raise ValueError("Pi Agent 内部令牌不能为空")
        self.base_url = base_url.rstrip("/")
        self._token = token
        # 事件流与普通请求的超时语义不同：普通请求是"一次调用多久算超时"，
        # 事件流是"两帧之间多久没动静算断流"。共用一个值会让长角色被误判。
        self._stream_read_timeout = stream_read_timeout
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
            transport=transport,
        )

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self._token}", "accept": "application/json"}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, headers=self._headers(), **kwargs)
            if response.status_code >= 400:
                raise _http_error(response)
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise PiAgentProtocolError("Pi Agent 返回的 JSON 无效") from exc
        except PiAgentProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary error is always redacted
            raise _redacted_error(exc, self._token) from exc

    def health(self) -> dict[str, Any]:
        value = self._request("GET", "/internal/v1/health")
        if not isinstance(value, dict):
            raise PiAgentProtocolError("Pi Agent health 响应不是对象")
        try:
            return PiHealthResponse.model_validate(value).model_dump(mode="json", exclude_none=True)
        except Exception as exc:
            raise PiAgentProtocolError("Pi Agent health 响应结构无效") from exc

    def create_run(self, run_id: str, request: PiAgentRequest | Mapping[str, Any]) -> dict[str, Any]:
        body = request.model_dump(mode="json") if isinstance(request, PiAgentRequest) else dict(request)
        value = self._request("PUT", f"/internal/v1/runs/{run_id}", json=body)
        if not isinstance(value, dict):
            raise PiAgentProtocolError("Pi Agent 创建任务响应不是对象")
        try:
            return PiRunResponse.model_validate(value).model_dump(mode="json", exclude_none=True)
        except Exception as exc:
            raise PiAgentProtocolError("Pi Agent 创建任务响应结构无效") from exc

    def status(self, run_id: str) -> dict[str, Any]:
        value = self._request("GET", f"/internal/v1/runs/{run_id}")
        if not isinstance(value, dict):
            raise PiAgentProtocolError("Pi Agent 状态响应不是对象")
        try:
            return PiRunResponse.model_validate(value).model_dump(mode="json", exclude_none=True)
        except Exception as exc:
            raise PiAgentProtocolError("Pi Agent 状态响应结构无效") from exc

    def result(self, run_id: str) -> dict[str, Any]:
        value = self._request("GET", f"/internal/v1/runs/{run_id}/result")
        if not isinstance(value, dict):
            raise PiAgentProtocolError("Pi Agent 结果响应不是对象")
        return value

    def result_model(self, run_id: str, request: PiAgentRequest) -> PiJudgmentResult:
        return validate_judgment_result(self.result(run_id), request, run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        value = self._request("POST", f"/internal/v1/runs/{run_id}/cancel")
        if not isinstance(value, dict):
            raise PiAgentProtocolError("Pi Agent 取消响应不是对象")
        try:
            return PiRunResponse.model_validate(value).model_dump(mode="json", exclude_none=True)
        except Exception as exc:
            raise PiAgentProtocolError("Pi Agent 取消响应结构无效") from exc

    def events(self, run_id: str, *, after_source_seq: int = 0) -> Iterator[dict[str, Any]]:
        if after_source_seq < 0:
            raise ValueError("after_source_seq 不可为负数")
        try:
            # 流读超时按 Node 侧心跳口径给（心跳 15 秒），不再沿用整段业务超时：
            # 健康流每 15 秒必有一帧，长角色不会被误杀；真断流则在一个心跳窗口内暴露。
            with self._client.stream(
                "GET",
                f"/internal/v1/runs/{run_id}/events",
                params={"after_source_seq": after_source_seq},
                headers={**self._headers(), "accept": "text/event-stream"},
                timeout=httpx.Timeout(
                    self._stream_read_timeout,
                    connect=min(15.0, self._stream_read_timeout),
                ),
                ) as response:
                if response.status_code >= 400:
                    raise PiAgentProtocolError(f"Pi Agent SSE HTTP {response.status_code}")
                data_lines: list[str] = []
                event_name: str | None = None
                event_id: str | None = None
                for line in response.iter_lines():
                    if line == "":
                        if data_lines:
                            payload = "\n".join(data_lines)
                            data_lines = []
                            if payload == "[DONE]":
                                return
                            try:
                                event = json.loads(payload)
                            except ValueError as exc:
                                raise PiAgentProtocolError("Pi Agent SSE 数据不是 JSON") from exc
                            if not isinstance(event, dict):
                                raise PiAgentProtocolError("Pi Agent SSE 事件不是对象")
                            if event_name and "event_type" not in event:
                                event["event_type"] = event_name
                            if event_id and "source_seq" not in event:
                                try:
                                    event["source_seq"] = int(event_id)
                                except ValueError as exc:
                                    raise PiAgentProtocolError("Pi Agent SSE 序号无效") from exc
                            event_name = None
                            event_id = None
                            yield event
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].lstrip()
                        continue
                    if line.startswith("id:"):
                        event_id = line[3:].lstrip()
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                if data_lines and "[DONE]" not in data_lines:
                    raise PiAgentProtocolError("Pi Agent SSE 流在事件结束前断开")
        except PiAgentProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary error is always redacted
            raise _redacted_error(exc, self._token) from exc

    def start_judgment(
        self, request: PiAgentRequest, *, run_id: str | None = None
    ) -> str:
        """Create an idempotent Pi run using a caller-owned business ID when given."""
        business_run_id = run_id or secrets.token_urlsafe(18)
        response = self.create_run(business_run_id, request)
        return str(response["run_id"])

    def get_status(self, run_id: str) -> dict[str, Any]:
        return self.status(run_id)

    def stream_events(self, run_id: str, after_seq: int = 0) -> Iterator[dict[str, Any]]:
        return self.events(run_id, after_source_seq=after_seq)

    def get_result(self, run_id: str, request: PiAgentRequest) -> PiJudgmentResult:
        return self.result_model(run_id, request)

    def close(self) -> None:
        self._client.close()


@dataclass
class PiAgentProcessHandle:
    process: Any
    client: PiAgentClient
    token: str
    base_url: str
    _closed: bool = False

    def terminate(self) -> None:
        terminate = getattr(self.process, "terminate", None)
        if callable(terminate):
            terminate()
        # 只发信号不等待的话，端口可能还没释放下一次启动就开始了，
        # 于是撞上"端口被占"。等它真的退出，超时再强杀。
        wait = getattr(self.process, "wait", None)
        if not callable(wait):
            return
        try:
            wait(timeout=5)
        except Exception:
            kill = getattr(self.process, "kill", None)
            if callable(kill):
                kill()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.client.close()
        self.terminate()


class PiAgentProcessSupervisor:
    """Owns the Node child-process boundary for the FastAPI lifespan."""

    def __init__(
        self,
        workdir: Path,
        *,
        process_factory: Callable[[list[str], dict[str, str], Path], Any] | None = None,
        node_executable: str = "node",
        model_api_key_env: str = "WORKBENCH_AI_API_KEY",
        readiness_timeout: float = 10.0,
        readiness_poll_interval: float = 0.05,
        readiness_probe_timeout: float = 1.0,
        request_timeout: float = 180.0,
    ) -> None:
        self.workdir = Path(workdir)
        self.node_executable = node_executable
        self.model_api_key_env = model_api_key_env
        self.process_factory = process_factory or self._default_process_factory
        self.readiness_timeout = readiness_timeout
        self.readiness_poll_interval = readiness_poll_interval
        self.readiness_probe_timeout = readiness_probe_timeout
        self.request_timeout = request_timeout
        self.handle: PiAgentProcessHandle | None = None
        # 子进程崩掉后按原参数重启用；密钥只留在内存里，不写日志也不出异常。
        self._launch: dict[str, Any] | None = None

    @staticmethod
    def _default_process_factory(command: list[str], env: dict[str, str], cwd: Path) -> Any:
        # 不能让子进程继承父进程的 stdout/stderr。父进程跑在 pty 里时，pty 一被
        # 回收，子进程写日志就拿到 EPIPE 直接崩，而且什么痕迹都不留。写到文件
        # 既切断这个依赖，也让崩溃原因有地方可查。
        log_path = Path(cwd) / "pi_agent.log"
        log_file = log_path.open("a", encoding="utf-8", buffering=1)
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )

    def start(
        self,
        *,
        base_url: str,
        model_api_key: str,
        internal_token: str | None = None,
        model_base_url: str | None = None,
        model_name: str | None = None,
    ) -> PiAgentProcessHandle:
        token = internal_token or secrets.token_urlsafe(32)
        if not model_api_key:
            raise ValueError("模型 API 密钥不能为空")
        parsed = httpx.URL(base_url)
        if parsed.host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Pi Agent 只能绑定本机地址")
        port = parsed.port
        if port is None:
            raise ValueError("Pi Agent base_url 必须包含固定端口")
        # 端口固定，上一次的 node 子进程若没被回收（服务被强杀时 close() 不会执行）
        # 会一直占着它。此时子进程只会以退出码 1 结束，真实原因被埋掉，
        # 因此启动前先探一次，把「端口被占」直接说清楚。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
            probe_socket.settimeout(1.0)
            if probe_socket.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(
                    f"端口 {port} 已被占用，Pi Agent 无法启动。"
                    "通常是上一次服务未正常关停留下的 node 子进程，"
                    f"先结束占用 {port} 的进程再重启。"
                )
        command = [self.node_executable, "--import", "tsx", "src/main.ts", "--host", "127.0.0.1", "--port", str(port)]
        env = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
            if os.environ.get(key)
        }
        env[self.model_api_key_env] = model_api_key
        env["PI_AGENT_API_KEY"] = model_api_key
        env["PI_AGENT_BASE_URL"] = model_base_url or "https://api.pie-xian.com/v1"
        env["PI_AGENT_MODEL"] = model_name or "minimax-m3"
        env["PI_AGENT_INTERNAL_TOKEN"] = token
        env["PI_AGENT_TOKEN"] = token
        env["PI_AGENT_PORT"] = str(port)
        # 父进程被强杀时 close() 不会执行，子进程就会一直占着固定端口，
        # 下次启动必然冲突。让子进程自己盯着这个 PID，父进程没了就退出。
        env["PI_AGENT_PARENT_PID"] = str(os.getpid())
        process = self.process_factory(command, env, self.workdir)
        # 业务客户端：真实模型单个角色要跑几十秒，实测 SSE 事件间隔可达 45 秒，
        # 超时必须按业务口径给。这个客户端会交给 AgentJudgeManager 与
        # PipelineManager 长期持有，绝不能沿用就绪探测的短超时。
        client = PiAgentClient(base_url, token, timeout=self.request_timeout)
        handle = PiAgentProcessHandle(process, client, token, base_url)
        # 探活客户端独立且短超时：健康检查该快速失败快速重试，用完即关。
        probe = PiAgentClient(base_url, token, timeout=self.readiness_probe_timeout)
        deadline = time.monotonic() + self.readiness_timeout
        last_error: BaseException | None = None
        try:
            while True:
                exit_code = process.poll()
                if exit_code is not None:
                    raise RuntimeError(f"Pi Agent 子进程启动后退出，退出码 {exit_code}")
                try:
                    probe.health()
                    self.handle = handle
                    self._launch = {"base_url": base_url, "model_api_key": model_api_key}
                    return handle
                except PiAgentProtocolError as exc:
                    last_error = exc
                if time.monotonic() >= deadline:
                    detail = f"：{last_error}" if last_error else ""
                    raise RuntimeError(f"Pi Agent 就绪超时{detail}")
                time.sleep(self.readiness_poll_interval)
        except Exception:
            handle.close()
            self.handle = None
            raise
        finally:
            probe.close()

    def ensure_alive(self) -> PiAgentProcessHandle | None:
        """确认子进程还活着，死了就用原参数重启。

        可用状态是启动那一刻的快照，子进程之后崩掉不会有人改它。不复检的话，
        系统会一直宣称 Pi Agent 可用，直到真正调用才炸出 502。
        """
        if self.handle is None:
            return None
        if self.handle.process.poll() is None:
            return self.handle
        logger.warning("Pi Agent 子进程已退出，正在按原参数重启")
        self.handle = None
        if self._launch is None:
            return None
        return self.start(**self._launch)

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


__all__ = ["PiAgentClient", "PiAgentProcessHandle", "PiAgentProcessSupervisor", "PiAgentProtocolError"]
