"""解析器用例：python tests/test_parse.py（不需要 Qt）。"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 19, 15, 30, tzinfo=TZ)  # 周四下午

CASES = [
    ("周五 17:00 交实验报告", (11, 20, 17, 0), 2, "交实验报告"),
    ("明天上午9点半 组会", (11, 20, 9, 30), 2, "组会"),
    ("下周一 8点 交作业", (11, 23, 8, 0), 2, "交作业"),
    ("11月28日 09:30 实验室", (11, 28, 9, 30), 2, "实验室"),
    ("11-25 20:30 组会", (11, 25, 20, 30), 2, "组会"),
    ("3天后 交材料", (11, 22, 23, 59), 2, "交材料"),
    ("周日 19:30-21:30 开发", (11, 22, 19, 30), 2, "开发"),
    ("今天晚上8点 复习高数", (11, 19, 20, 0), 2, "复习高数"),
    ("明天 9点 交作业 !高", (11, 20, 9, 0), 3, "交作业"),
    ("周五 交报告", (11, 20, 23, 59), 2, "交报告"),
    ("交水电费", None, 2, "交水电费"),
]


def main() -> int:
    try:
        from taskboard import parse  # 需要 PySide6 已安装
    except Exception:
        # 只装了标准库也能测：把解析函数单独摘出来
        import importlib.util
        src = Path(__file__).resolve().parent.parent / "taskboard.py"
        text = src.read_text(encoding="utf-8")
        start = text.index("WEEKDAY = {")
        end = text.index("def bucket(")
        ns: dict = {}
        exec(compile("import re\nfrom datetime import datetime, timedelta\n"
                     "from zoneinfo import ZoneInfo\nTZ = ZoneInfo('Asia/Shanghai')\n"
                     + text[start:end], "parse_excerpt", "exec"), ns)
        parse = ns["parse"]

    bad = 0
    for raw, want_time, want_prio, want_title in CASES:
        title, due, prio = parse(raw, NOW)
        got = (due.month, due.day, due.hour, due.minute) if due else None
        ok = got == want_time and prio == want_prio and title == want_title
        bad += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + f"{raw!r} -> {got} p{prio} {title!r}"
              + ("" if ok else f"  期望 {want_time} p{want_prio} {want_title!r}"))
    print(f"\n{len(CASES) - bad}/{len(CASES)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
