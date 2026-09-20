import ast
import os
import subprocess

import yaml

# 安全实现:literal_eval(对照)
def safe_expr(expr):
    return ast.literal_eval(expr)

# 安全实现:无 shell 调用(对照)
def safe_list():
    return subprocess.run(["ls", "-l"], check=True)

# 安全实现:SafeLoader(对照)
def safe_yaml(text):
    return yaml.safe_load(text)

# 安全实现:环境变量读取(对照)
API_KEY = os.environ.get("API_KEY", "")
