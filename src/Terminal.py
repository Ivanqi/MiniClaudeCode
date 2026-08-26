"""终端输出工具:线程安全的打印与彩色状态行。

后台线程打印时会先清行、再重绘 CLI 输入提示符,
避免输出与用户正在输入的内容混在一起。
"""
import threading

try:
    import readline
    readline.parse_and_bind("set bind-tty-special-chars off")
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False

from src.Configs import Configs


class Terminal:
    """终端输出工具(全部为类方法,无需实例化)。"""

    # ANSI 颜色码
    GRAY = "\033[90m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    RESET = "\033[0m"

    @classmethod
    def print(cls, text: str):
        """打印一行文本;非主线程打印时会重绘输入提示符。"""
        if threading.current_thread() is threading.main_thread() or not Configs.CLI_ACTIVE:
            print(text)
            return
        line = ""
        if READLINE_AVAILABLE:
            try:
                line = readline.get_line_buffer()
            except Exception:
                line = ""
        print(f"\r\033[K{text}")
        print(Configs.PROMPT + line, end="", flush=True)

    @classmethod
    def info(cls, tag: str, text: str, color: str = CYAN):
        """打印带彩色标签的状态行,如 `  [claim] task → in_progress`。"""
        cls.print(f"  {color}[{tag}] {text}{cls.RESET}")
