"""LLM 接入层：只做「模型调用 + 失败兜底」，不掺业务逻辑。

配置全部来自环境变量（也支持项目根目录的 ``.env``）：

| 变量 | 说明 | 默认 |
|---|---|---|
| ``LLM_PROVIDER`` | 提供方标识 | ``deepseek`` |
| ``LLM_API_KEY`` | API Key（兼容旧的 ``AGENT_LLM_API_KEY``） | — |
| ``LLM_BASE_URL`` | OpenAI 兼容网关地址 | DeepSeek ``https://api.deepseek.com`` |
| ``LLM_MODEL`` | 模型名 | ``deepseek-chat`` |
| ``AGENT_LLM_TIMEOUT`` | 单次超时秒数 | 45 |
| ``AGENT_LLM_MAX_TOKENS`` | 最大输出 token | 1600 |
| ``AGENT_LLM_DISABLED`` | 设为 ``1`` 强制走规则模板（演示/测试用） | 0 |

**没有 Key 不是错误**：``available()`` 返回 False，报告节点自动降级为规则模板，
并在返回结构里写明 ``mode: template``。绝不在没有调用模型时声称是模型写的。
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any

_USAGE = threading.local()


def reset_usage() -> None:
    _USAGE.value = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                    "call_count": 0, "failed_call_count": 0}


def usage_summary() -> dict:
    return dict(getattr(_USAGE, "value", {}) or {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
        "call_count": 0, "failed_call_count": 0})


def _record_usage(resp=None, *, failed: bool = False) -> None:
    row = usage_summary()
    row["call_count"] += 1
    row["failed_call_count"] += int(failed)
    usage = getattr(resp, "usage", None)
    if usage:
        row["input_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
        row["output_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
        row["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)
    _USAGE.value = row

try:                                    # .env 可选；装不上也不影响运行
    from dotenv import load_dotenv
    load_dotenv()
except Exception:                       # pragma: no cover
    pass

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


class LLMError(Exception):
    """模型调用失败：调用方负责降级，不把异常抛给用户。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"type": self.code, "message": self.message}


def _key() -> str:
    for name in ("LLM_API_KEY", "AGENT_LLM_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return ""


def _base_url() -> str:
    v = (os.environ.get("LLM_BASE_URL") or os.environ.get("AGENT_LLM_BASE_URL") or "").strip()
    if v:
        return v.rstrip("/")
    if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("AGENT_LLM_API_KEY"):
        return DEFAULT_BASE_URL
    return (os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    return (os.environ.get("LLM_MODEL") or os.environ.get("AGENT_LLM_MODEL")
            or os.environ.get("OPENAI_MODEL")
            or DEFAULT_MODEL).strip()


def _disabled() -> bool:
    return (os.environ.get("AGENT_LLM_DISABLED") or "").strip() in ("1", "true", "yes")


def available() -> bool:
    """是否具备真实调用条件（有 Key、没被显式禁用、openai SDK 可用）。"""
    if _disabled() or not _key():
        return False
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def config_summary() -> dict:
    """给前端/健康检查用的配置摘要——**绝不返回 Key 本身**。"""
    base = _base_url()
    return {
        "available": available(),
        "model": _model(),
        "base_url": base,
        "provider": (os.environ.get("LLM_PROVIDER") or _provider(base)).strip(),
        "key_configured": bool(_key()),
        "disabled_by_env": _disabled(),
        "sdk": _sdk_version(),
        "env_vars": ["LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"],
    }


def _provider(base: str) -> str:
    if "deepseek" in base:
        return "DeepSeek（OpenAI 兼容）"
    if "openai.com" in base:
        return "OpenAI"
    if "dashscope" in base or "aliyun" in base:
        return "通义千问（OpenAI 兼容）"
    return "OpenAI 兼容网关"


def _sdk_version() -> str:
    try:
        from importlib.metadata import version
        return f"openai {version('openai')}"
    except Exception:                   # pragma: no cover
        return "openai 未安装"


def _client():
    from openai import OpenAI
    timeout = float(os.environ.get("AGENT_LLM_TIMEOUT") or 45)
    return OpenAI(api_key=_key(), base_url=_base_url(), timeout=timeout, max_retries=1)


def chat(messages: list[dict], *, json_mode: bool = True,
         temperature: float | None = None, max_tokens: int | None = None) -> str:
    """调用对话补全，返回纯文本。失败抛 ``LLMError``（调用方决定是否降级）。"""
    if not available():
        raise LLMError("NotConfigured",
                       "未配置模型（缺少 LLM_API_KEY，或已被 AGENT_LLM_DISABLED 禁用）")
    kwargs: dict[str, Any] = {
        "model": _model(),
        "messages": messages,
        "temperature": (float(os.environ.get("AGENT_LLM_TEMPERATURE") or 0.2)
                        if temperature is None else temperature),
        "max_tokens": int(max_tokens or os.environ.get("AGENT_LLM_MAX_TOKENS") or 1600),
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        client = _client()
        resp = client.chat.completions.create(**kwargs)
        _record_usage(resp)
        text = (resp.choices[0].message.content or "").strip()
    except Exception as exc:            # openai 的各类异常统一收敛
        _record_usage(failed=True)
        name = type(exc).__name__
        hint = ""
        if "response_format" in str(exc) or "json_object" in str(exc):
            hint = "（该模型可能不支持 JSON 模式）"
        raise LLMError(name, f"{exc}{hint}")
    if not text:
        raise LLMError("EmptyResponse", "模型返回空内容")
    return text


def chat_json(messages: list[dict], *, temperature: float | None = None,
              max_tokens: int | None = None) -> dict:
    """调用并解析 JSON；模型偶尔包 ```json 代码块，这里做一次剥离。

    JSON 模式不被支持时（部分网关）自动重试一次「纯文本 + 提示只要 JSON」。
    """
    text = chat(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
    data = _parse_json(text)
    if data is not None:
        return data
    text = chat(messages + [{"role": "user", "content": "只输出合法 JSON，不要任何解释。"}],
                json_mode=False, temperature=temperature, max_tokens=max_tokens)
    data = _parse_json(text)
    if data is None:
        raise LLMError("InvalidJSON", f"模型输出不是合法 JSON：{text[:120]}")
    return data


def _parse_json(text: str) -> dict | None:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t.lstrip("`")
        t = t.lstrip("json").strip() if t.lower().startswith("json") else t
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
