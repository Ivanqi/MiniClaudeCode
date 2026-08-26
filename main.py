#!/usr/bin/env python3
"""MiniClaudeCode 入口:DeepSeek 驱动的编码代理。

运行:  python main.py
需要:  pip install -r requirements.txt + .env 中的 DEEPSEEK_API_KEY
"""
from dotenv import load_dotenv

load_dotenv(override=True)

from src.AgentLoop import AgentLoop
from src.Configs import Configs
from src.LlmClient import DeepSeekClient


def main():
    Configs.CLI_ACTIVE = True
    client = DeepSeekClient.from_env()
    loop = AgentLoop(client)
    loop.run_cli()


if __name__ == "__main__":
    main()
