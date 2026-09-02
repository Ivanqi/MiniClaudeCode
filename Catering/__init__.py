"""Catering 餐饮业务包:门店经营异常分析 Agent。

基于 src 基础设施(LLM 客户端、工具注册表、终端、恢复链)构建,
单向依赖 Catering -> src。模块禁止 import dotenv 或构造真实 LLM 客户端,
环境初始化只发生在 main.py,保证测试全程离线。
"""
