import datetime
import os
import sys
import traceback

COLOR_ENABLED = sys.stdout.isatty() and "NO_COLOR" not in os.environ

palette = {
    "错误": ((255, 46, 99), (255, 46, 99)),
    "警告": ((249, 237, 105), (249, 237, 105)),
    "信息": ((48, 227, 202), (255, 255, 255)),
    "调试": ((82, 97, 107), (82, 97, 107)),
}
RESET = "\033[0m"

# setup 可改写；默认写到工作目录的 Log.txt
path = "Log.txt"
quiet = False


def setup(new_path, new_quiet=False):
    # 文件始终记录调试信息；quiet 时控制台只显示信息及以上
    global path, quiet
    path = str(new_path)
    quiet = new_quiet
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)


def tint(color, text):
    if not COLOR_ENABLED:
        return text
    r, g, b = color
    return f"\033[38;2;{r};{g};{b}m{text}{RESET}"


def write(level, message):
    timestamp = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")
    with open(path, "a", encoding="utf-8") as file:
        file.write(f"{timestamp} {level} {message}\n")
    if quiet and level == "调试":
        return
    label, text = palette[level]
    print(f"{timestamp} {tint(label, level)} {tint(text, message)}")


# 把字节数格式化为可读大小
def human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024


def debug(message):
    write("调试", message)


def info(message):
    write("信息", message)


def warn(message):
    write("警告", message)


def error(message, exc=False):
    if exc:
        message += "\n" + traceback.format_exc().rstrip()
    write("错误", message)
