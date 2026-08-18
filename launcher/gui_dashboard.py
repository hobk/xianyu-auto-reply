"""
GUI仪表盘页面模块

当前仅保留本地占位内容，不再请求任何远程广告接口。
"""
import tkinter as tk

from launcher.gui_theme import COLORS


def render_dashboard_page(app):
    content = app._content_frame

    canvas = tk.Canvas(content, bg=COLORS["card_bg"], highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)

    inner = tk.Frame(canvas, bg=COLORS["card_bg"])
    canvas.create_window((0, 0), window=inner, anchor=tk.NW)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    tk.Label(
        inner,
        text="仪表盘",
        font=("微软雅黑", 14, "bold"),
        fg=COLORS["text"],
        bg=COLORS["card_bg"],
    ).pack(anchor=tk.W, padx=20, pady=(16, 4))
    tk.Label(
        inner,
        text="当前版本仅展示本地内容，不加载外部广告和公告。",
        font=("微软雅黑", 10),
        fg=COLORS["text_secondary"],
        bg=COLORS["card_bg"],
    ).pack(anchor=tk.W, padx=20, pady=(0, 12))

    card = tk.Frame(inner, bg="#ffffff", highlightbackground="#e2e8f0", highlightthickness=1)
    card.pack(fill=tk.X, padx=16, pady=(0, 16))
    body = tk.Frame(card, bg="#ffffff", padx=16, pady=16)
    body.pack(fill=tk.BOTH, expand=True)
    tk.Label(
        body,
        text="已关闭远程广告展示",
        font=("微软雅黑", 12, "bold"),
        fg=COLORS["text"],
        bg="#ffffff",
    ).pack(anchor=tk.W)
    tk.Label(
        body,
        text="此页面保留为本地信息页，后续可继续放置系统状态或快捷入口。",
        font=("微软雅黑", 9),
        fg=COLORS["text_secondary"],
        bg="#ffffff",
        wraplength=720,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(6, 0))
