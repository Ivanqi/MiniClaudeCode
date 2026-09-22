#!/usr/bin/env python3
"""MiniClaudeCode 入口:DeepSeek 驱动的编码代理。

运行:  python main.py
需要:  pip install -r requirements.txt + .env 中的 DEEPSEEK_API_KEY
"""
from dotenv import load_dotenv

load_dotenv(override=True)

from src.AgentLoop import AgentLoop
from src.Configs import Configs
from src.ModelRouter import ModelRouter


def main():
    Configs.CLI_ACTIVE = True
    # 混合路由:api_client(deepseek-chat)+ local_client(24GB 机上 vLLM 服务的微调小模型)
    # 未配置 LOCAL_BASE_URL/LOCAL_MODEL_ID 时退化为纯 API 行为
    router = ModelRouter.from_env()
    loop = AgentLoop(router.api_client, router=router)
    loop.run_cli()


if __name__ == "__main__":
    main()
