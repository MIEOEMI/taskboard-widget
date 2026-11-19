#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面任务挂件（Windows / Ubuntu 通用）。

贴在壁纸上的半透明看板：置底、不进任务栏、不抢焦点、可拖动、可锁定穿透。
数据直连自建 Vikunja（今天 / 未来 7 天 / 以后 三列），10 秒自动同步。

    python taskboard.py            # 正常启动
    python taskboard.py --selftest # 无界面自检（CI / 服务器上验证渲染与逻辑）

凭据优先级：环境变量 → ~/.config/vikunja/env → 脚本同目录 .env
    VIKUNJA_URL / VIKUNJA_USER / VIKUNJA_PASS / VIKUNJA_PROJECT
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QPoint, QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget,
)

TZ = ZoneInfo("Asia/Shanghai")
REFRESH_MS = 10_000
COLUMNS = (("today", "今天"), ("soon", "未来 7 天"), ("later", "以后"))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config() -> dict[str, str]:
    cfg = {
        "VIKUNJA_URL": os.environ.get("VIKUNJA_URL", ""),
        "VIKUNJA_USER": os.environ.get("VIKUNJA_USER", ""),
        "VIKUNJA_PASS": os.environ.get("VIKUNJA_PASS", ""),
        "VIKUNJA_PROJECT": os.environ.get("VIKUNJA_PROJECT", "2"),
    }
    if not cfg["VIKUNJA_URL"]:
        candidates = [
            os.path.expanduser("~/.config/vikunja/env"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
            os.path.join(os.environ.get("APPDATA", ""), "vikunja", "env"),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                for line in open(path, encoding="utf-8"):
                    line = line.strip()
                    if line and "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        cfg[k.strip()] = v.strip()
                if cfg["VIKUNJA_URL"]:
                    break
    return cfg


# --------------------------------------------------------------------------- #
# Vikunja 数据层（只用标准库，断网不崩）
# --------------------------------------------------------------------------- #
class Vikunja:
    def __init__(self, cfg: dict[str, str]) -> None:
        self.base = (cfg.get("VIKUNJA_URL") or "").rstrip("/")
        self.user = cfg.get("VIKUNJA_USER", "")
        self.password = cfg.get("VIKUNJA_PASS", "")
        self.project = int(cfg.get("VIKUNJA_PROJECT") or 2)
        self.token: str | None = None
        self.last_error = ""

    def _call(self, method: str, path: str, body=None, auth: bool = True):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
        )
        req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else {}

    def login(self) -> bool:
        if not self.base:
            self.last_error = "缺少 VIKUNJA_URL"
            return False
        try:
            self.token = self._call(
                "POST", "/api/v1/login",
                {"username": self.user, "password": self.password}, auth=False,
            )["token"]
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = f"登录失败：{exc}"
            return False

    def tasks(self) -> list[dict] | None:
        """未完成任务；失败返回 None（调用方保留旧数据并显示离线）。"""
        if not self.token and not self.login():
            return None
        try:
            out, page = [], 1
            while True:  # 接口每页最多 50 条，必须翻页
                batch = self._call("GET", f"/api/v1/projects/{self.project}/tasks?page={page}")
                if not batch:
                    break
                out += batch
                if len(batch) < 50:
                    break
                page += 1
            self.last_error = ""
            return [t for t in out if not t.get("done")]
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):  # token 过期就重登一次
                if self.login():
                    return self.tasks()
            self.last_error = f"读取失败：{exc}"
            return None
        except Exception as exc:
            self.last_error = f"离线：{exc}"
            return None

    def add(self, title: str, due: datetime | None, priority: int = 2) -> bool:
        if not self.token and not self.login():
            return False
        body = {"title": title, "priority": priority}
        if due:
            body["due_date"] = due.isoformat()
        try:
            self._call("PUT", f"/api/v1/projects/{self.project}/tasks", body)
            return True
        except Exception as exc:
            self.last_error = f"添加失败：{exc}"
            return False

    def set_done(self, task: dict, done: bool = True) -> bool:
        """更新走 POST，且是全量替换：漏掉的字段会被清空。"""
        if not self.token and not self.login():
            return False
        body = {
            "title": task["title"],
            "description": task.get("description") or "",
            "due_date": task.get("due_date"),
            "priority": task.get("priority", 0),
            "project_id": self.project,
            "done": done,
        }
        try:
            self._call("POST", f"/api/v1/tasks/{task['id']}", body)
            return True
        except Exception as exc:
            self.last_error = f"勾选失败：{exc}"
            return False


# --------------------------------------------------------------------------- #
# 一句话解析：今天/明天/周X/!高
# --------------------------------------------------------------------------- #
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
TIME_RE = re.compile(
    r"(?:(?P<prefix>今天|明天|后天|大后天|下周|本周|这周|周|星期)(?P<wd>[一二三四五六日天])?)?"
    r"\s*(?:(?P<mon>\d{1,2})[月/](?P<day>\d{1,2})[日号]?|(?P<mon2>\d{1,2})-(?P<day2>\d{1,2})(?![\d:：]))?"
    r"\s*(?:(?P<rel>\d+)\s*天后)?"
    r"\s*(?P<period>上午|早上|早晨|中午|下午|傍晚|晚上)?"
    r"\s*(?:(?P<hh>\d{1,2})(?:(?P<half>点半)|(?P<sep>[:：点时])(?:(?P<mm>\d{1,2})\s*分?)?)?"
    r"(?:\s*[-~—–至到]\s*\d{1,2}(?:(?:[:：点时])\d{1,2}\s*分?)?)?)?"
)
PERIOD_ADD = {"下午": 12, "傍晚": 12, "晚上": 12, "夜里": 12, "中午": 12}


def parse(text: str, now: datetime | None = None) -> tuple[str, datetime | None, int]:
    """「明天 9点 交作业 !高」→ (标题, 截止时间, 优先级)。解析不出日期就返回 None。"""
    now = now or datetime.now(TZ)
    priority = 2
    if "!高" in text:
        priority, text = 3, text.replace("!高", "")
    elif "!低" in text:
        priority, text = 1, text.replace("!低", "")
    text = text.strip()

    m = TIME_RE.search(text)
    if not m or not any(m.group(g) for g in ("prefix", "mon", "mon2", "rel", "hh")):
        return text, None, priority

    today = now.date()
    day = None
    prefix, wd = m.group("prefix"), m.group("wd")
    if prefix in ("今天", "明天", "后天", "大后天"):
        day = today + timedelta(days={"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[prefix])
    elif prefix and wd:
        target = WEEKDAY[wd]
        if prefix == "下周":
            day = today + timedelta(days=(7 - today.weekday()) + target)
        else:
            day = today + timedelta(days=(target - today.weekday()) % 7)
    elif m.group("mon") or m.group("mon2"):
        mon = int(m.group("mon") or m.group("mon2"))
        dd = int((m.group("day") or m.group("day2")) or 1)
        try:
            day = today.replace(year=today.year + (1 if mon < today.month - 6 else 0),
                                month=mon, day=dd)
        except ValueError:
            day = None
    elif m.group("rel"):
        day = today + timedelta(days=int(m.group("rel")))
    if day is None:
        return text, None, priority

    if m.group("hh"):
        hour = int(m.group("hh")) + PERIOD_ADD.get(m.group("period") or "", 0)
        minute = 30 if m.group("half") else int(m.group("mm") or 0)
        hour = hour - 12 if hour >= 24 else hour
    else:
        hour, minute = 23, 59
    due = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    title = (text[: m.start()] + " " + text[m.end():]).strip(" ，,、-~—") or text
    return title, due, priority


def bucket(task: dict, now: datetime) -> str:
    due = task.get("due_date")
    if not due:
        return "later"
    d = datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone(TZ)
    if d.date() <= now.date():
        return "today"
    if d.date() <= now.date() + timedelta(days=7):
        return "soon"
    return "later"


def due_label(task: dict, now: datetime) -> tuple[str, bool]:
    due = task.get("due_date")
    if not due:
        return "", False
    d = datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone(TZ)
    delta = (d.date() - now.date()).days
    hm = d.strftime("%H:%M") if (d.hour, d.minute) != (23, 59) else ""
    if delta < 0:
        return f"逾期{-delta}天 {hm}".strip(), True
    if delta == 0:
        return f"{hm}".strip(), False
    return f"{d.month}/{d.day} {hm}".strip(), False


# --------------------------------------------------------------------------- #
# 挂件窗口
# --------------------------------------------------------------------------- #
class Widget(QWidget):
    def __init__(self, api: Vikunja, now_provider=datetime.now) -> None:
        super().__init__()
        self.api = api
        self.settings = QSettings("Hermes", "TaskBoard")
        self.tasks: list[dict] = []
        self.locked = False

        # ---- 桌面挂件三件套：无边框 / 不进任务栏 / 永远沉底 ----
        self.setWindowFlags(
            Qt.FramelessWindowHint      # 无标题栏
            | Qt.Tool                   # 不进任务栏、不出现在 Alt-Tab
            | Qt.WindowStaysOnBottomHint  # 置底：永不盖住其他软件
        )
        self.setAttribute(Qt.WA_TranslucentBackground)   # 圆角外透明
        self.setAttribute(Qt.WA_ShowWithoutActivating)   # 显示时不抢焦点
        self.resize(int(self.settings.value("w", 720)), int(self.settings.value("h", 430)))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame(self)
        panel.setObjectName("panel")
        panel.setStyleSheet(
            # 关键：QWidget 默认会用调色板底色铺一块（浅色主题下是白的），必须压成透明，
            # 否则深色面板上会浮出一条条白底、浅色文字全被吃掉。
            "QWidget { background: transparent; }"
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
            "#panel { background: rgba(20, 24, 33, 232); border-radius: 16px;"
            " border: 1px solid rgba(255,255,255,28); }"
            "QLabel { color: #e7eaf0; }"
            "QLabel#dim { color: #8b93a3; }"
            "QLabel#warn { color: #ff6b6b; }"
            "QCheckBox { color: #e7eaf0; spacing: 8px; }"
            "QCheckBox::indicator { width: 13px; height: 13px; border-radius: 4px;"
            " border: 1px solid rgba(255,255,255,90); background: rgba(255,255,255,10); }"
            "QCheckBox::indicator:hover { border-color: #4c9f70; }"
            "QCheckBox::indicator:checked { background: #4c9f70; border-color: #4c9f70; }"
            "QLineEdit { background: rgba(255,255,255,16); border: none; border-radius: 8px;"
            "  padding: 6px 10px; color: #e7eaf0; }"
            "QScrollArea { border: none; background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 2px; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,45); border-radius: 3px;"
            " min-height: 24px; }"
            "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }"
        )
        outer.addWidget(panel)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)

        # ---- 顶栏 ----
        head = QHBoxLayout()
        self.title = QLabel("我的一天")
        self.title.setFont(QFont(self.font().family(), 13, QFont.Bold))
        self.count = QLabel("")
        self.count.setObjectName("dim")
        self.add_box = QLineEdit()
        self.add_box.setPlaceholderText("一句话添加：明天 9点 交作业 !高")
        self.add_box.setFixedWidth(280)
        self.add_box.returnPressed.connect(self.on_add)
        head.addWidget(self.title)
        head.addWidget(self.count)
        head.addStretch(1)
        head.addWidget(self.add_box)
        lay.addLayout(head)

        # ---- 三列 ----
        cols = QHBoxLayout()
        cols.setSpacing(14)
        self.bodies: dict[str, QVBoxLayout] = {}
        for key, name in COLUMNS:
            col = QVBoxLayout()
            col.setSpacing(4)
            head_lbl = QLabel(name)
            head_lbl.setObjectName("dim")
            col.addWidget(head_lbl)
            body = QVBoxLayout()
            body.setSpacing(2)
            holder = QWidget()
            holder.setLayout(body)
            holder.setAttribute(Qt.WA_TranslucentBackground)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidget(holder)
            scroll.viewport().setAutoFillBackground(False)
            scroll.setStyleSheet("background: transparent; border: none;")
            col.addWidget(scroll, 1)
            self.bodies[key] = body
            cols.addLayout(col, 1)
        lay.addLayout(cols, 1)

        self.status = QLabel("")
        self.status.setObjectName("dim")
        lay.addWidget(self.status)

        # ---- 拖动 ----
        self._drag_off: QPoint | None = None

    # -- 拖动 --------------------------------------------------------------- #
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self.locked:
            self._drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_off is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_off)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        self._drag_off = None
        self.settings.setValue("pos", self.pos())
        super().mouseReleaseEvent(e)

    # -- 渲染 --------------------------------------------------------------- #
    def clear(self, layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def render_task(self, layout: QVBoxLayout, task: dict) -> None:
        row = QFrame()
        row.setObjectName("row")
        row.setStyleSheet(
            "#row { background: transparent; border: none;"
            " border-bottom: 1px solid rgba(255,255,255,18); }")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 4, 0, 4)
        rl.setSpacing(8)
        cb = QCheckBox()
        cb.setChecked(False)
        cb.setCursor(Qt.PointingHandCursor)
        cb.toggled.connect(lambda checked, t=task: self.on_done(t, checked))
        text = html.escape(task["title"])
        stamp, overdue = due_label(task, self.now())
        # 颜色必须写死在 HTML 里：富文本用的是调色板颜色，深色面板上会变成几乎不可见的深灰
        markup = f"<span style='color:#e7eaf0'>{text}</span>"
        if stamp:
            markup += f"&nbsp;&nbsp;<span style='color:{'#ff6b6b' if overdue else '#8b93a3'}'>{stamp}</span>"
        lbl = QLabel(markup)
        lbl.setTextFormat(Qt.RichText)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("font-size: 12px; line-height: 140%;")
        rl.addWidget(cb, 0, Qt.AlignTop)
        rl.addWidget(lbl, 1, Qt.AlignTop)
        layout.addWidget(row)

    def render(self) -> None:
        now = self.now()
        groups: dict[str, list[dict]] = {k: [] for k, _ in COLUMNS}
        for t in self.tasks:
            groups[bucket(t, now)].append(t)
        for key, _ in COLUMNS:
            body = self.bodies[key]
            self.clear(body)
            for t in sorted(groups[key], key=lambda x: x.get("due_date") or "9999"):
                self.render_task(body, t)
            if not groups[key]:
                empty = QLabel("—")
                empty.setObjectName("dim")
                body.addWidget(empty)
            body.addStretch(1)
        self.count.setText(f"{len(self.tasks)} 项未完成")

    def now(self) -> datetime:
        return datetime.now(TZ)

    # -- 交互 --------------------------------------------------------------- #
    def on_done(self, task: dict, checked: bool) -> None:
        if not checked:
            return
        if self.api.set_done(task, True):
            self.tasks = [t for t in self.tasks if t["id"] != task["id"]]
            self.render()
        else:
            self.status.setText(self.api.last_error)

    def on_add(self) -> None:
        raw = self.add_box.text().strip()
        if not raw:
            return
        title, due, prio = parse(raw, self.now())
        if self.api.add(title, due, prio):
            self.add_box.clear()
            self.status.setText(f"已添加：{title}")
            self.refresh()
        else:
            self.status.setText(self.api.last_error)

    def refresh(self) -> None:
        data = self.api.tasks()
        if data is None:
            self.status.setText(self.api.last_error or "离线，10 秒后重试")
            self.status.setObjectName("warn")
            self.status.setStyleSheet("color:#ff6b6b;")
            return
        self.tasks = data
        self.status.setStyleSheet("color:#8b93a3;")
        self.status.setText(f"已同步 {datetime.now(TZ):%H:%M:%S}")
        self.render()

    # -- 锁定（点击穿透，纯展示） -------------------------------------------- #
    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        self.setAttribute(Qt.WA_TransparentForMouseEvents, locked)
        if sys.platform == "win32":
            self._win_click_through(locked)
        self.status.setText("已锁定（点击穿透）" if locked else "已解锁")

    def _win_click_through(self, on: bool) -> None:
        try:
            import ctypes
            hwnd = int(self.winId())
            GWL_EXSTYLE, WS_EX_TRANSPARENT, WS_EX_LAYERED = -20, 0x20, 0x80000
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style = style | WS_EX_TRANSPARENT | WS_EX_LAYERED if on else style & ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def set_opacity(self, value: float) -> None:
        self.setWindowOpacity(value)
        self.settings.setValue("opacity", value)


# --------------------------------------------------------------------------- #
# 托盘 + 启动
# --------------------------------------------------------------------------- #
def make_icon() -> QIcon:
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setBrush(QColor("#4c9f70"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(3, 3, 26, 26, 7, 7)
    p.setPen(QColor("white"))
    p.drawLine(9, 16, 23, 16)
    p.end()
    return QIcon(pix)


def run(selftest: bool = False) -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(not selftest)
    api = Vikunja(load_config())
    api.login()

    w = Widget(api)
    w.refresh()                       # 先拉一次真实数据（失败也不崩）
    w.move(w.settings.value("pos", QPoint(80, 80)))
    w.setWindowOpacity(float(w.settings.value("opacity", 0.96)))

    if selftest:
        print("flags frameless:", bool(w.windowFlags() & Qt.FramelessWindowHint))
        print("flags tool:", bool(w.windowFlags() & Qt.Tool))
        print("flags bottom:", bool(w.windowFlags() & Qt.WindowStaysOnBottomHint))
        print("translucent:", w.testAttribute(Qt.WA_TranslucentBackground))
        print("no-activate:", w.testAttribute(Qt.WA_ShowWithoutActivating))
        print("tasks loaded:", len(w.tasks), "| api error:", api.last_error or "-")
        w.show()
        app.processEvents()
        img = w.grab()
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selftest.png")
        img.save(out)
        print("screenshot:", out, img.width(), "x", img.height())
        # 解析器自检
        for sample in ("明天 9点 交作业 !高", "周五 17:00 交实验报告", "周会 无日期"):
            print("parse", sample, "->", parse(sample, datetime(2026, 11, 19, 15, 30, tzinfo=TZ)))
        return 0

    tray = QSystemTrayIcon(make_icon(), app)
    menu = QMenu()
    act_lock = QAction("锁定（点击穿透）", menu, checkable=True)
    act_lock.toggled.connect(w.set_locked)
    menu.addAction(act_lock)
    op_menu = menu.addMenu("透明度")
    for label, val in (("100%", 1.0), ("96%", 0.96), ("85%", 0.85), ("70%", 0.70)):
        a = QAction(label, op_menu)
        a.triggered.connect(lambda _=False, v=val: w.set_opacity(v))
        op_menu.addAction(a)
    menu.addSeparator()
    act_quit = QAction("退出", menu)
    act_quit.triggered.connect(app.quit)
    menu.addAction(act_quit)
    tray.setContextMenu(menu)
    tray.setToolTip("Hermes 任务挂件")
    tray.show()

    timer = QTimer()
    timer.timeout.connect(w.refresh)
    timer.start(REFRESH_MS)

    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run("--selftest" in sys.argv))
