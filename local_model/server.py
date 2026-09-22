"""本地小模型 OpenAI 兼容服务(4GB 开发机验证用 shim)。

llama-cpp-python 自带的 OpenAI 服务不会解析 Qwen 模板输出的
<tool_call> 文本块(嵌入式模板路径下 tool_calls 恒为 None)。
本服务用 GGUF 内嵌 Qwen 模板做提示词构造,自己把模型输出里的
<tool_call> 块解析成标准 OpenAI tool_calls 结构,供
MiniClaudeCode 的 DeepSeekClient 直接对接。

正式部署(24GB 机 + QLoRA 微调模型)用 vLLM 替换本服务,
接口一致,无需改动 MiniClaudeCode 代码。

用法(conda 环境 llama 内):
    python local_model/server.py \
        --model /root/models/qwen2.5-3b-instruct-q4_k_m.gguf \
        --port 8080

MiniClaudeCode 侧配置(.env):
    LOCAL_BASE_URL=http://127.0.0.1:8080/v1
    LOCAL_MODEL_ID=qwen2.5-3b-instruct-q4_k_m   # 本服务忽略 model 字段,填任意名
"""
import argparse
import json
import re
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from llama_cpp import Llama

# Qwen 模板输出的工具调用块:<tool_call>{"name": ..., "arguments": {...}}</tool_call>
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def parse_tool_calls(content: str):
    """从模型输出中提取 <tool_call> 块,返回 (tool_calls, 剩余文本)。

    关键设计:JSON 合法 → 参数以解析后的 JSON 上报(工具直接执行);
    JSON 非法但能认出 name → 以原始字符串上报,由 LlmClient 标记
    parse_error,MiniClaudeCode 的升级阶梯接管(重试→升级 API)。
    小模型的坏 JSON 不能静默留在 content 里当正常文本收尾。
    """
    tool_calls, cleaned = [], content or ""
    for match in TOOL_CALL_RE.finditer(content or ""):
        raw = match.group(1).strip()
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', raw)
        name = name_match.group(1) if name_match else ""
        try:
            block = json.loads(raw)
        except json.JSONDecodeError:
            # Qwen 模板在部分渲染路径下会输出双层花括号 {{...}},剥一层再试
            block = None
            if raw.startswith("{{") and raw.endswith("}}"):
                try:
                    block = json.loads(raw[1:-1])
                except json.JSONDecodeError:
                    pass
        if block is not None:
            name = block.get("name") or name
            arguments = json.dumps(block.get("arguments") or {},
                                   ensure_ascii=False)
        else:
            # 非法 JSON:原样上报参数,由上层 parse_error 机制处理
            arguments = raw
        if name:
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:16]}",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            })
            cleaned = cleaned.replace(match.group(0), "", 1)
    if not tool_calls and "<tool_call" in (content or ""):
        # 截断/格式异常且未匹配到完整块:整体上报,触发上层升级
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', content)
        if name_match:
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:16]}",
                "type": "function",
                "function": {"name": name_match.group(1),
                             "arguments": content},
            })
            cleaned = ""
    return tool_calls, cleaned.strip()


app = FastAPI()


@app.get("/health")
def health():
    """健康检查端点,供 ModelRouter 探测(30 秒缓存)。"""
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI 兼容 /v1/chat/completions(非流式),支持 tools。"""
    body = await request.json()
    messages = body.get("messages", [])
    tools = body.get("tools")
    max_tokens = int(body.get("max_tokens", 512))
    response = app.state.llm.create_chat_completion(
        messages=messages, tools=tools, max_tokens=max_tokens)
    choice = response["choices"][0]
    message = choice.get("message") or {}
    tool_calls, cleaned = parse_tool_calls(message.get("content") or "")
    return JSONResponse({
        "id": response.get("id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response.get("model"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": cleaned,
                        "tool_calls": tool_calls or None},
            "finish_reason": choice.get("finish_reason", "stop"),
        }],
    })


def main():
    parser = argparse.ArgumentParser(description="Local OpenAI-compatible LLM server")
    parser.add_argument("--model", required=True, help="GGUF 模型路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--n-ctx", type=int, default=4096, help="上下文长度")
    parser.add_argument("--n-gpu-layers", type=int, default=0,
                        help="GPU 卸载层数(4GB 显存建议 0,CPU 推理)")
    args = parser.parse_args()
    # 不指定 chat_format:使用 GGUF 内嵌的 Qwen 模板(工具调用格式正确)
    app.state.llm = Llama(model_path=args.model, n_ctx=args.n_ctx,
                          n_gpu_layers=args.n_gpu_layers, verbose=False)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
