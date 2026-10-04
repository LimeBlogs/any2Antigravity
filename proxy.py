"""
Antigravity IDE 协议专职网关 (Gemini API -> OpenAI/DeepSeek)
依赖: pip install fastapi uvicorn httpx
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger("any2antigravity")

# ==================== 环境变量加载 ====================
_BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """从 .env 载入键值对，不覆盖已存在的环境变量。

    优先复用已安装的 python-dotenv；否则回退到内置轻量解析器（无额外依赖）。
    支持 `KEY=VALUE`、带引号的值、`export` 前缀与 `#` 注释。
    """
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(path, override=False)
        return
    except ImportError:
        pass
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip("'\""))


def _env(name: str, default: str) -> str:
    """读取环境变量，空值回退到默认值。"""
    return os.getenv(name) or default


_load_dotenv(_BASE_DIR / ".env")

# ==================== 配置区 (优先级: 环境变量 > .env > 默认值) ====================
OFFICIAL_UPSTREAM = _env("OFFICIAL_UPSTREAM", "https://cloudcode-pa.googleapis.com")

# 你的第三方模型配置 (支持 DeepSeek、OpenRouter、OneAPI/NewAPI 等)
TARGET_BASE_URL = _env("TARGET_BASE_URL", "https://api.deepseek.com/v1")
TARGET_API_KEY = _env("TARGET_API_KEY", "")
TARGET_MODEL = _env("TARGET_MODEL", "deepseek-chat")  # 若用推理模型可填 deepseek-reasoner

# System 指令中展示给模型的自定义身份名
LOCAL_MODEL_NAME = _env("LOCAL_MODEL_NAME", "Claude Opus 4.8 (Max)")

# 思考模式参数形态: none(默认, 大多数推理模型无需显式开启) / deepseek / openrouter
THINKING_STYLE = _env("THINKING_STYLE", "none").strip().lower()

# 是否把历史 assistant 的 reasoning_content 回传上游(多数推理模型建议关闭, DeepSeek 会直接报错)
SEND_REASONING_CONTENT = _env("SEND_REASONING_CONTENT", "false").strip().lower() in {"1", "true", "yes", "on"}

# 监听地址与服务端口
HOST = _env("HOST", "127.0.0.1")
PORT = int(_env("PORT", "9099"))

# System 指令中形如 "Gemini 3.6 Flash (High)" 的官方模型标识将被统一替换为 LOCAL_MODEL_NAME
# 结构: {{模型名}} {{版本号}} {{思考强度}}，其中版本号与思考强度均可省略
_MODEL_FAMILY = r"(?:gemini|claude|gpt|o[1-9]|qwen|deepseek|glm|grok|mistral|llama)"
_MODEL_VARIANT = r"(?:pro|flash|ultra|nano|mini|max|opus|sonnet|haiku|turbo|preview|thinking|reasoning|latest)"
_MODEL_THINKING = r"(?:high|medium|low|minimal|none|dynamic)"
# 版本号：支持 3、3.6、4o、20241022 等形态
_MODEL_TOKEN = rf"(?:\d+(?:\.\d+)*\w*|{_MODEL_VARIANT}|{_MODEL_THINKING})"
_MODEL_NAME_RE = re.compile(
    rf"\b{_MODEL_FAMILY}"
    rf"(?:[\s-]+{_MODEL_TOKEN})+"  # 版本号 / 变体后缀 / 思考强度(至少一个, 避免误伤 gemini cli / gpt2 等普通词)
    rf"(?:[\s-]*\(\s*{_MODEL_THINKING}\s*\))?",  # 带括号的思考强度
    re.IGNORECASE,
)
# ===============================================

# 转发时应剥离的逐跳头部，避免破坏链路语义
_FORWARD_HOP_HEADERS = {"host", "content-length"}
_RESPONSE_HOP_HEADERS = {"content-encoding", "content-length"}

# 允许参与"连续同角色合并"的角色集合
_MERGEABLE_ROLES = {"user", "system", "assistant"}

# 上游流式读取超时策略：连接/写入/池 15s，读取 180s
_STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=15.0, pool=15.0)
# 官方直通接口的整体超时
_PASSTHROUGH_TIMEOUT = 60.0

# OpenAI finish_reason -> Gemini finishReason 的映射（如实透传截断/审查，避免 IDE 误判为正常结束）
_FINISH_REASON_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "content_filter": "SAFETY",
    "tool_calls": "STOP",
    "function_call": "STOP",
}


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """复用单一连接池，避免每个请求重建 TCP/TLS 连接。"""
    app.state.client = httpx.AsyncClient(timeout=_STREAM_TIMEOUT, verify=True)
    try:
        yield
    finally:
        await app.state.client.aclose()


app = FastAPI(lifespan=_lifespan)


# ==================== 通用工具 ====================
def _join_text(*chunks: Optional[str]) -> str:
    """以空行拼接多段文本并去除首尾空白（自动忽略空段）。"""
    return "\n\n".join(c for c in chunks if c).strip()


# ==================== 请求转译：Gemini -> OpenAI ====================
def sanitize_schema_types(schema: Any) -> Any:
    """递归将 Google Protobuf 的全大写 Schema 类型转换为标准 JSON Schema 小写类型"""
    if isinstance(schema, dict):
        return {
            k: (v.lower() if k == "type" and isinstance(v, str) else sanitize_schema_types(v))
            for k, v in schema.items()
        }
    if isinstance(schema, list):
        return [sanitize_schema_types(item) for item in schema]
    return schema


def _localize_model_name(text: str) -> str:
    """将 System 指令中的官方模型名称（含版本号与思考强度）替换为自定义身份名。"""
    return _MODEL_NAME_RE.sub(lambda _: LOCAL_MODEL_NAME, text)


def _extract_system_message(gemini_req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """提取 systemInstruction 为一条 system 消息，并本地化其中的模型名称。"""
    parts = gemini_req.get("systemInstruction", {}).get("parts", [])
    text = "\n".join(p["text"] for p in parts if "text" in p)
    return {"role": "system", "content": _localize_model_name(text)} if text else None


def _to_openai_media_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将 Gemini inlineData / fileData 图片或附件转换为 OpenAI 多模态 content part。"""
    inline = part.get("inlineData") or part.get("inline_data")
    if inline:
        mime = inline.get("mimeType") or inline.get("mime_type") or "application/octet-stream"
        data = inline.get("data", "")
        data_url = f"data:{mime};base64,{data}"
        if mime.startswith("image/"):
            return {"type": "image_url", "image_url": {"url": data_url}}
        suffix = mime.split("/")[-1] or "bin"
        return {"type": "file", "file": {"filename": f"attachment.{suffix}", "file_data": data_url}}
    filed = part.get("fileData") or part.get("file_data")
    if filed:
        uri = filed.get("fileUri") or filed.get("file_uri") or ""
        mime = filed.get("mimeType") or filed.get("mime_type") or ""
        if not uri:
            return None
        if mime.startswith("image/") or "image" in uri.lower():
            return {"type": "image_url", "image_url": {"url": uri}}
        return {"type": "file", "file": {"filename": uri.rsplit("/", 1)[-1], "file_data": uri}}
    return None


def _parse_part(part: Dict[str, Any]) -> Tuple[str, Any]:
    """将单个 part 归一化为 (kind, payload)，兼容 camelCase 与 snake_case。"""
    text = part.get("text", "")
    if text:
        return ("thought" if part.get("thought") else "text"), text
    if call := (part.get("functionCall") or part.get("function_call")):
        return "call", call
    if response := (part.get("functionResponse") or part.get("function_response")):
        return "response", response
    if media := _to_openai_media_part(part):
        return "media", media
    return "empty", None


class _IdAllocator:
    """为无 id 的 Gemini 工具调用分配唯一 id，并按名称与响应顺序配对。"""

    def __init__(self) -> None:
        self._counter = 0
        self._pending: Dict[str, List[str]] = {}

    def register_call(self, call: Dict[str, Any]) -> str:
        name = call.get("name", "tool")
        cid = call.get("id")
        if not cid:
            cid = f"call_{name}_{self._counter}"
            self._counter += 1
        self._pending.setdefault(name, []).append(cid)
        return cid

    def resolve_response(self, response: Dict[str, Any]) -> str:
        if response.get("id"):
            return response["id"]
        name = response.get("name", "tool")
        queue = self._pending.get(name)
        if queue:
            return queue.pop(0)
        return f"call_{name}"


def _to_openai_tool_call(call: Dict[str, Any], allocator: "_IdAllocator") -> Dict[str, Any]:
    """将 Gemini functionCall 转换为 OpenAI tool_call 结构，并登记 id 以便与响应配对。"""
    name = call.get("name", "tool")
    args = call.get("args", {})
    return {
        "id": allocator.register_call(call),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
        },
    }


def _to_tool_message(response: Dict[str, Any], allocator: "_IdAllocator") -> Dict[str, Any]:
    """将 Gemini functionResponse 转换为 OpenAI tool 消息，并回填对应的 call id。"""
    name = response.get("name", "tool")
    payload = response.get("response", {})
    return {
        "role": "tool",
        "tool_call_id": allocator.resolve_response(response),
        "content": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
    }


def _build_turn_messages(turn: Dict[str, Any], out: List[Dict[str, Any]], allocator: "_IdAllocator") -> None:
    """解析单个 contents 轮次，并将生成的消息追加到 out。"""
    role = "assistant" if turn.get("role") == "model" else "user"
    text_chunks: List[str] = []
    thought_chunks: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    media_parts: List[Dict[str, Any]] = []

    for part in turn.get("parts", []):
        kind, payload = _parse_part(part)
        if kind == "text":
            text_chunks.append(payload)
        elif kind == "thought":
            thought_chunks.append(payload)
        elif kind == "media":
            media_parts.append(payload)
        elif kind == "call":
            tool_calls.append(_to_openai_tool_call(payload, allocator))
        elif kind == "response":
            out.append(_to_tool_message(payload, allocator))

    text = _join_text(*text_chunks)
    if role == "assistant":
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": text or None,
            "reasoning_content": _join_text(*thought_chunks) or "...",
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        out.append(message)
    elif media_parts:
        # 多模态用户消息：文本 + 图片/附件，按 OpenAI content 数组下发
        content: List[Dict[str, Any]] = []
        if text:
            content.append({"type": "text", "text": _localize_model_name(text)})
        content.extend(media_parts)
        out.append({"role": "user", "content": content})
    elif text:
        out.append({"role": "user", "content": _localize_model_name(text)})


def _merge_content(prev_content: Any, curr_content: Any) -> Any:
    """拼接两条消息的 content，兼容纯文本与多模态 content 数组。"""
    if isinstance(prev_content, list) or isinstance(curr_content, list):
        def _as_list(value: Any) -> List[Dict[str, Any]]:
            if not value:
                return []
            return value if isinstance(value, list) else [{"type": "text", "text": value}]
        return _as_list(prev_content) + _as_list(curr_content)
    return _join_text(prev_content, curr_content)


def _merge_into(prev: Dict[str, Any], curr: Dict[str, Any]) -> None:
    """就地熔合两个连续的同角色消息（含正文、思考链与工具调用）。"""
    role = prev.get("role")
    if role in ("user", "system"):
        prev["content"] = _merge_content(prev.get("content"), curr.get("content"))
    elif role == "assistant":
        prev["content"] = _join_text(prev.get("content"), curr.get("content"))
        prev["reasoning_content"] = _join_text(prev.get("reasoning_content"), curr.get("reasoning_content"))
        combined_tools = (prev.get("tool_calls") or []) + (curr.get("tool_calls") or [])
        if combined_tools:
            prev["tool_calls"] = combined_tools


def _finalize_assistant(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """校验 assistant 消息的合法性，返回清洗后的消息；无内容则返回 None。"""
    if not (msg.get("content") or "").strip() and not msg.get("tool_calls"):
        if not msg.get("reasoning_content"):
            return None  # 彻底无任何内容的死节点直接丢弃
        msg["content"] = "..."  # 有思考链但无正文时补全保底正文
    if not msg.get("reasoning_content"):
        msg["reasoning_content"] = "..."  # 思考模式下 reasoning_content 必须存在
    return msg


def _sanitize_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剔除没有对应工具结果的 tool_calls，以及没有对应调用的 tool 消息。"""
    responded = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            kept = [tc for tc in msg["tool_calls"] if tc.get("id") in responded]
            if kept:
                msg["tool_calls"] = kept
            else:
                msg.pop("tool_calls", None)

    known_calls = {tc.get("id") for m in messages for tc in (m.get("tool_calls") or [])}
    result: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool" and msg.get("tool_call_id") not in known_calls:
            continue  # 丢弃无对应工具调用的结果
        if role == "assistant" and not msg.get("tool_calls") and not str(msg.get("content") or "").strip():
            continue  # 丢弃彻底为空的 assistant 节点
        result.append(msg)
    return result


def clean_and_consolidate_messages(raw_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """深度合并多轮连续相同角色的消息，并确保满足 OpenAI/DeepSeek 严格校验"""
    consolidated: List[Dict[str, Any]] = []
    for msg in raw_messages:
        if not msg:
            continue
        prev = consolidated[-1] if consolidated else None
        # 不跨越工具调用边界合并，避免把两组独立的 tool_calls 错拼在一起
        can_merge = (
            prev is not None
            and msg.get("role") in _MERGEABLE_ROLES
            and prev.get("role") == msg.get("role")
            and not prev.get("tool_calls")
            and not msg.get("tool_calls")
        )
        if can_merge:
            _merge_into(prev, msg)
        else:
            consolidated.append(msg)

    final_messages: List[Dict[str, Any]] = []
    for msg in consolidated:
        if msg.get("role") != "assistant":
            final_messages.append(msg)
            continue
        if (finalized := _finalize_assistant(msg)) is not None:
            final_messages.append(finalized)

    # 修剪末尾多余的空 assistant 节点（如有）
    while (
        final_messages
        and final_messages[-1].get("role") == "assistant"
        and not final_messages[-1].get("tool_calls")
        and final_messages[-1].get("content") == "..."
    ):
        final_messages.pop()

    return _sanitize_tool_pairs(final_messages)


def _build_openai_tools(gemini_req: Dict[str, Any]) -> List[Dict[str, Any]]:
    """将 Gemini tools 声明洗标为 OpenAI 兼容的 tools 结构。"""
    tools: List[Dict[str, Any]] = []
    for group in gemini_req.get("tools", []):
        for decl in group.get("functionDeclarations", []):
            params = decl.get("parameters", {"type": "object", "properties": {}})
            tools.append({
                "type": "function",
                "function": {
                    "name": decl.get("name", ""),
                    "description": decl.get("description", ""),
                    "parameters": sanitize_schema_types(params),
                },
            })
    return tools


_THINKING_PARAMS: Dict[str, Dict[str, Any]] = {
    "deepseek": {"thinking": {"type": "enabled"}},
    "openrouter": {"reasoning": {"enabled": True}},
    "none": {},
}


def _thinking_params() -> Dict[str, Any]:
    """按配置返回上游思考模式的顶层参数（不同上游写法不同）。"""
    return dict(_THINKING_PARAMS.get(THINKING_STYLE, {}))


def _apply_generation_config(payload: Dict[str, Any], gc: Dict[str, Any]) -> None:
    """将 Gemini generationConfig 映射为 OpenAI 采样参数。"""
    mapping = {
        "temperature": "temperature",
        "topP": "top_p",
        "top_p": "top_p",
        "maxOutputTokens": "max_tokens",
        "max_output_tokens": "max_tokens",
        "stopSequences": "stop",
        "stop_sequences": "stop",
    }
    for src, dst in mapping.items():
        if gc.get(src) is not None:
            payload[dst] = gc[src]


def transform_gemini_request(gemini_req: Dict[str, Any]) -> Dict[str, Any]:
    """将 Antigravity 的 Gemini 协议完整转译为 OpenAI/DeepSeek 兼容结构"""
    raw_messages: List[Dict[str, Any]] = []
    allocator = _IdAllocator()

    if system := _extract_system_message(gemini_req):
        raw_messages.append(system)

    for turn in gemini_req.get("contents", []):
        _build_turn_messages(turn, raw_messages, allocator)

    messages = clean_and_consolidate_messages(raw_messages)
    if not SEND_REASONING_CONTENT:
        for msg in messages:
            msg.pop("reasoning_content", None)

    payload: Dict[str, Any] = {
        "model": TARGET_MODEL,
        "messages": messages,
        "stream": True,
    }
    payload.update(_thinking_params())
    _apply_generation_config(
        payload,
        gemini_req.get("generationConfig") or gemini_req.get("generation_config") or {},
    )
    if tools := _build_openai_tools(gemini_req):
        payload["tools"] = tools

    return payload


# ==================== 响应转译：OpenAI -> Gemini SSE ====================
def _sse(packet: Dict[str, Any]) -> str:
    return f"data: {json.dumps(packet, ensure_ascii=False)}\n\n"


def _wrap_candidate(part: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
    """将单个 content part 封装为 Antigravity 前端可解析的 SSE 数据包。"""
    candidate: Dict[str, Any] = {"content": {"parts": [part], "role": "model"}, "index": 0}
    if finish_reason:
        candidate["finishReason"] = finish_reason
    return {"candidates": [candidate], "response": {"candidates": [candidate]}}


def _map_finish_reason(reason: Optional[str]) -> Optional[str]:
    """把上游 OpenAI finish_reason 映射为 Gemini finishReason；未知值回退 None。"""
    if not reason:
        return None
    return _FINISH_REASON_MAP.get(reason.lower(), "STOP")


def make_gemini_sse(text: str = "", is_thought: bool = False, finish_reason: Optional[str] = None) -> str:
    """封装文本（含思考链）的 SSE 格式块"""
    part = {"thought": True, "text": text} if is_thought else {"text": text}
    return _sse(_wrap_candidate(part, finish_reason))


def make_gemini_tool_sse(tool_name: str, arguments: str, finish_reason: Optional[str] = None, call_id: Optional[str] = None) -> str:
    """封装工具调用的 SSE 响应块；透传 id 以便后续 functionResponse 正确配对。"""
    try:
        parsed_args = json.loads(arguments)
    except Exception:
        parsed_args = {"raw_input": arguments}
    call: Dict[str, Any] = {"name": tool_name, "args": parsed_args}
    if call_id:
        call["id"] = call_id
    return _sse(_wrap_candidate({"functionCall": call}, finish_reason))


async def _translate_stream(response: httpx.Response) -> AsyncGenerator[str, None]:
    """逐行读取上游 SSE，转译为 Gemini SSE；结束时收敛工具调用并如实映射 finishReason。"""
    pending_tools: Dict[int, Dict[str, str]] = {}
    tool_ids: Dict[int, str] = {}
    finish_reason: Optional[str] = None

    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue
        data = line.removeprefix("data: ").strip()
        if data == "[DONE]":
            break

        try:
            choice = json.loads(data)["choices"][0]
            delta = choice.get("delta", {})
        except Exception:
            continue

        # 捕获上游结束原因（stop / length / content_filter / tool_calls）
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

        # 捕获思考链 (DeepSeek-R1 / Qwen-Thinking 等)
        if reasoning := delta.get("reasoning_content"):
            yield make_gemini_sse(reasoning, is_thought=True)

        # 捕获主文本
        if content := delta.get("content"):
            yield make_gemini_sse(content)

        # 捕获工具调用帧（按 index 分桶，arguments 可能跨多帧分片）
        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            entry = pending_tools.setdefault(idx, {"name": "", "args": ""})
            if tc.get("id"):
                tool_ids[idx] = tc["id"]
            function = tc.get("function", {})
            if function.get("name"):
                entry["name"] = function["name"]
            if function.get("arguments"):
                entry["args"] += function["arguments"]

    final_reason = _map_finish_reason(finish_reason) or "STOP"
    calls = [(idx, entry) for idx, entry in sorted(pending_tools.items()) if entry["name"]]
    if calls:
        last = len(calls) - 1
        for i, (idx, call) in enumerate(calls):
            yield make_gemini_tool_sse(
                call["name"],
                call["args"],
                final_reason if i == last else None,
                tool_ids.get(idx),
            )
    else:
        yield make_gemini_sse("", finish_reason=final_reason)


async def proxy_stream_to_upstream(client: httpx.AsyncClient, openai_req: Dict[str, Any]) -> AsyncGenerator[str, None]:
    """向上游发起流式请求，逐块封装为 Gemini SSE 格式推送给 IDE"""
    headers = {
        "Authorization": f"Bearer {TARGET_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{TARGET_BASE_URL.rstrip('/')}/chat/completions"

    try:
        async with client.stream("POST", url, headers=headers, json=openai_req, timeout=_STREAM_TIMEOUT) as response:
            if response.status_code != 200:
                err_msg = (await response.aread()).decode("utf-8", errors="ignore")
                yield make_gemini_sse(
                    f"上游模型接口返回错误 ({response.status_code}): {err_msg}",
                    finish_reason="STOP",
                )
                return
            async for sse in _translate_stream(response):
                yield sse
    except Exception as e:
        logger.exception("网关转发异常")
        yield make_gemini_sse(f"\n[网关转发异常]: {e}", finish_reason="STOP")


# ==================== 路由与转发 ====================
async def _handle_generate(request: Request) -> Response:
    """拦截核心模型生成请求，转译为第三方接口。"""
    logger.info("[ROUTE] 拦截到核心模型调用请求，转译为第三方接口...")
    try:
        body = json.loads((await request.body()).decode("utf-8"))
        openai_payload = transform_gemini_request(body.get("request", {}))
    except Exception as exc:
        logger.exception("请求转换异常")
        return Response(status_code=500, content=str(exc))

    return StreamingResponse(
        proxy_stream_to_upstream(request.app.state.client, openai_payload),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _passthrough(request: Request, path: str) -> Response:
    """状态、鉴权与其余接口：无缝直通 Google。

    注意：此处刻意透传客户端原始 Authorization 凭证给官方上游（设计意图），
    仅用于 Google 的鉴权/配额/模型列表等接口，与第三方 TARGET_API_KEY 无关。
    采用流式转发避免大响应体一次性读入内存。
    """
    logger.info("[PASS-THROUGH] 直通官方: %s /%s", request.method, path)
    forward_headers = {k: v for k, v in request.headers.items() if k.lower() not in _FORWARD_HOP_HEADERS}
    client = request.app.state.client
    body = await request.body()
    try:
        upstream_req = client.build_request(
            method=request.method,
            url=f"{OFFICIAL_UPSTREAM}/{path}",
            headers=forward_headers,
            params=dict(request.query_params),
            content=body,
            timeout=_PASSTHROUGH_TIMEOUT,
        )
        upstream_resp = await client.send(upstream_req, stream=True)
    except Exception as e:
        logger.exception("直通上游异常")
        return Response(status_code=502, content=f"Upstream Error: {e}")

    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _RESPONSE_HOP_HEADERS}

    # 204 No Content / 304 Not Modified 不允许携带 body，直接返回空响应避免协议错误
    if upstream_resp.status_code in (204, 304):
        await upstream_resp.aclose()
        return Response(status_code=upstream_resp.status_code, headers=resp_headers)

    async def _body_stream() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in upstream_resp.aiter_bytes():
                yield chunk
        finally:
            await upstream_resp.aclose()

    return StreamingResponse(
        _body_stream(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def route_gateway(request: Request, path: str) -> Response:
    if "streamGenerateContent" in path:
        return await _handle_generate(request)
    return await _passthrough(request, path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(app, host=HOST, port=PORT)