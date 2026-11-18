"""零依赖 HTTP 服务：静态页面 + JSON 查询接口。"""
from __future__ import annotations

import logging
import math
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import config
from .api import MAX_BODY_BYTES, handle, handle_post, to_json_bytes

logger = logging.getLogger("bi_dashboard")


def _finite_clean(obj):
    """把 NaN / Inf 归一为 None，避免非法 JSON。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: _finite_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite_clean(v) for v in obj]
    return obj


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "BIDashboardV1/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(config.WEB_DIR), **kwargs)

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):                                    # noqa: N802
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            if u.path == "/favicon.ico":                 # 避免浏览器产生 404 控制台噪声
                self.send_response(204)
                self.end_headers()
                return None
            return super().do_GET()
        status, payload = handle(u.path, u.query)
        self._send_json(status, payload)

    def do_POST(self):                                   # noqa: N802
        """写入类接口（客户经理目标配置）+ AI 经营助手（只读分析）。"""
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"ok": False, "error": "请求体过大"})
            return None
        body = self.rfile.read(length) if length > 0 else b""

        # SSE：Agent 逐步产出事件，前端据此显示「正在查预算 / 正在算缺口」
        if u.path == "/api/agent/stream":
            return self._stream_agent(body)

        status, payload = handle_post(u.path, body,
                                      server_context=self._agent_server_context())
        self._send_json(status, payload)

    def _agent_server_context(self) -> dict:
        """从后端 HTTP 上下文选择 Demo 用户；请求 JSON 无权声明角色。"""
        return {
            "demo_user_id": (self.headers.get("X-Demo-User") or "demo_group_admin").strip(),
            "identity_source": "http_header" if self.headers.get("X-Demo-User")
                               else "backend_default",
        }

    def _stream_agent(self, body: bytes) -> None:
        """Server-Sent Events。任何异常都以 error 事件结束，不让连接悬着。"""
        import json as _json
        try:
            payload = _json.loads(body.decode("utf-8-sig") or "{}")
            question = str(payload.get("question") or "").strip()
            if not question:
                self._send_json(400, {"ok": False,
                                      "error": {"type": "RequestValidationError",
                                                "message": "question 不能为空"}})
                return None
            from agent.api import sse_lines
            from agent.schemas import AgentRequest
            req = AgentRequest.model_validate(payload)
        except Exception as exc:                       # 参数问题：普通 JSON 400
            self._send_json(400, {"ok": False,
                                  "error": {"type": type(exc).__name__,
                                            "message": str(exc)}})
            return None

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # 不用 chunked：发完就关连接，前端读到 EOF 即结束（SimpleHTTPRequestHandler 是 HTTP/1.0）
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self._cache_header_sent = True
        self.end_headers()
        self.close_connection = True
        try:
            for chunk in sse_lines(req.question, thread_id=req.thread_id,
                                   context=(req.context.model_dump() if req.context else None),
                                   history=req.history,
                                   server_context=self._agent_server_context(),
                                   request_id=req.request_id,
                                   parent_request_id=req.parent_request_id):
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return None                                  # 前端关掉页面，正常现象
        except Exception as exc:                         # pragma: no cover
            try:
                self.wfile.write(("event: error\ndata: "
                                  + _json.dumps({"error": str(exc)}, ensure_ascii=False)
                                  + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass
        return None


    def _send_json(self, status: int, payload: dict) -> None:
        body = to_json_bytes(_finite_clean(payload))
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self._cache_header_sent = True
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):                                   # noqa: N802
        return super().do_HEAD()

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        # 静态资源（html / js / css / 地图数据）默认不发 Cache-Control，
        # 浏览器会启发式缓存，改完代码刷新还看到旧版。本地开发一律 no-store：
        # 改完刷新即可见，不靠人工强刷。
        if not getattr(self, "_cache_header_sent", False):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self._cache_header_sent = True
        super().end_headers()


def make_server(host: str = config.HOST, port: int = config.PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    httpd.daemon_threads = True
    return httpd


def configure_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_DIR / "server.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
