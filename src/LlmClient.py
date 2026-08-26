"""DeepSeek LLM API 适配层。

DeepSeek 提供 OpenAI 兼容的 /chat/completions 接口,本模块负责:
1. 封装 HTTP 调用与鉴权、错误归一化(DeepSeekApiError);
2. 把 API 原始 JSON 转换成与协议无关的数据结构
   (LlmResponse / ToolCall),上层逻辑不感知具体 API 格式;
3. 提供消息与工具定义的构造辅助函数。

上层(AgentLoop、TeammateSystem、SubagentSystem 等)只面向:
- client.create(messages, system=..., tools=..., max_tokens=...) -> LlmResponse
- user_message / assistant_message / tool_message / make_function_tool
换用其他 OpenAI 兼容服务商时只需修改本模块的 base_url 与错误分类。
"""
import json
import os
from dataclasses import dataclass, field

import httpx


# ── 统一数据结构 ──

@dataclass
class ToolCall:
    """一次工具调用,与具体 API 格式无关。"""
    id: str
    name: str
    arguments: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict) -> "ToolCall":
        """从 API 返回的 tool_calls 条目构造。"""
        function = raw.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        return cls(id=raw.get("id") or "",
                   name=function.get("name") or "",
                   arguments=arguments)


@dataclass
class LlmResponse:
    """一次模型回复,与具体 API 格式无关。"""
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


# ── 消息与工具定义构造器 ──

def user_message(content: str) -> dict:
    """构造 user 角色消息。"""
    return {"role": "user", "content": content}


def assistant_message(response: LlmResponse) -> dict:
    """把一次回复转换成 assistant 角色消息(含 tool_calls,可原样回传 API)。"""
    message = {"role": "assistant", "content": response.content or ""}
    if response.tool_calls:
        message["tool_calls"] = [
            {"id": call.id, "type": "function",
             "function": {"name": call.name,
                          "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
            for call in response.tool_calls
        ]
    return message


def tool_message(tool_call_id: str, content: str) -> dict:
    """构造工具执行结果消息,与 assistant 消息中的 tool_calls 配对。"""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": str(content)}


def make_function_tool(name: str, description: str, parameters: dict) -> dict:
    """构造 OpenAI 风格的 function 工具定义。"""
    return {"type": "function",
            "function": {"name": name,
                         "description": description,
                         "parameters": parameters}}


# ── API 错误 ──

class DeepSeekApiError(Exception):
    """DeepSeek API 返回的错误(HTTP 4xx/5xx)。"""

    def __init__(self, status_code: int, body: str):
        super().__init__(f"DeepSeek API error {status_code}: {body[:500]}")
        self.status_code = status_code
        self.body = body


def classify_error(error: Exception) -> str:
    """把异常归类为 ratelimit / overload / other,供重试逻辑决策。"""
    if isinstance(error, DeepSeekApiError):
        if error.status_code == 429:
            return "ratelimit"
        if error.status_code in (500, 503, 529):
            return "overload"
    name = type(error).__name__.lower()
    message = str(error).lower()
    if "ratelimit" in name or "429" in message:
        return "ratelimit"
    if "overload" in name or "529" in message or "503" in message:
        return "overload"
    return "other"


# ── DeepSeek 客户端 ──

class DeepSeekClient:
    """DeepSeek chat/completions 客户端(基于 httpx,无额外 SDK 依赖)。"""

    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(self, api_key: str, base_url: str | None = None,
                 model: str | None = None, fallback_model: str | None = None,
                 timeout: float = 120.0):
        self._api_key = api_key
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.model = model or self.DEFAULT_MODEL
        self.fallback_model = fallback_model
        self._http = httpx.Client(timeout=timeout)  # 复用连接池

    @classmethod
    def from_env(cls) -> "DeepSeekClient":
        """从环境变量构建:必填 DEEPSEEK_API_KEY,其余可选。"""
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("Missing DEEPSEEK_API_KEY: set it in .env or environment")
        return cls(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL"),
            model=os.getenv("MODEL_ID"),
            fallback_model=os.getenv("FALLBACK_MODEL_ID"),
            timeout=float(os.getenv("LLM_TIMEOUT", "120")),
        )

    def create(self, messages: list[dict], system: str | None = None,
               tools: list[dict] | None = None, max_tokens: int = 4096,
               model: str | None = None) -> LlmResponse:
        """调用 chat/completions,返回归一化后的回复。

        注意:传入的 messages 不会被修改,system 会作为首条消息拼接。
        """
        payload = {
            "model": model or self.model,
            "messages": ([{"role": "system", "content": system}] if system else [])
                        + list(messages),
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        response = self._http.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            raise DeepSeekApiError(response.status_code, response.text)
        try:
            data = response.json()
        except ValueError:
            raise DeepSeekApiError(response.status_code, response.text)
        choice = data["choices"][0]
        message = choice.get("message") or {}
        return LlmResponse(
            content=message.get("content") or "",
            tool_calls=[ToolCall.from_api(raw) for raw in (message.get("tool_calls") or [])],
            finish_reason=choice.get("finish_reason") or "stop",
        )
