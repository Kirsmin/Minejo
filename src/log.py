import datetime

palette = {
    "错误": ((255, 46, 99),  (255, 46, 99)),
    "警告": ((249, 237, 105), (249, 237, 105)),
    "信息": ((48, 227, 202), (255, 255, 255)),
    "调试": ((82, 97, 107),  (82, 97, 107)),
}
reset = "\033[0m"

def tint(color, text):
    r, g, b = color
    return f"\033[38;2;{r};{g};{b}m{text}{reset}"

def write(level, message):
    timestamp = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")
    with open("Log.txt", "a", encoding="utf-8") as file:
        file.write(f"{timestamp} {level} {message}\n")
    label, text = palette[level]
    print(f"{timestamp} {tint(label, level)} {tint(text, message)}")
def debug(message): write("调试", message)
def info(message):  write("信息", message)
def warn(message):  write("警告", message)
def error(message): write("错误", message)

