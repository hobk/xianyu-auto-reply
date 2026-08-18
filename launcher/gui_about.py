"""
GUI关于页面模块

功能：
1. 作为左侧导航的"关于"菜单对应内容页
2. 显示系统版本信息
3. 使用本地二维码图片，不再从远程服务器加载
"""
from __future__ import annotations

import io
import tkinter as tk
import threading
from pathlib import Path

from launcher.gui_theme import COLORS
from launcher.version import CURRENT_VERSION

_QRCODE_ITEMS = [
    ("wechat-group.jpg", "微信群"),
    ("qq-group.jpg", "QQ群"),
    ("wechat-official-group.jpg", "公众号"),
    ("telegram-group.png", "Telegram"),
    ("reward-group.png", "赞赏码"),
]


def render_about_page(app):
    content = app._content_frame

    canvas = tk.Canvas(content, bg=COLORS["card_bg"], highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    inner = tk.Frame(canvas, bg=COLORS["card_bg"])
    canvas.create_window((0, 0), window=inner, anchor=tk.NW)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

    tk.Label(inner, text="关于", font=("微软雅黑", 14, "bold"),
             fg=COLORS["text"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20, pady=(16, 4))
    tk.Label(inner, text="闲鱼自动回复管理系统",
             font=("微软雅黑", 12), fg=COLORS["text"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20)
    tk.Label(inner, text=f"当前版本: v{CURRENT_VERSION}",
             font=("微软雅黑", 10), fg=COLORS["text_secondary"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20, pady=(0, 8))

    tk.Frame(inner, height=1, bg=COLORS["border"]).pack(fill=tk.X, padx=20, pady=8)
    tk.Label(inner, text="扫码关注 / 加群交流 / 赞赏支持",
             font=("微软雅黑", 10), fg=COLORS["text_secondary"], bg=COLORS["card_bg"]).pack(
        anchor=tk.W, padx=20)

    qr_frame = tk.Frame(inner, bg=COLORS["card_bg"])
    qr_frame.pack(fill=tk.X, padx=20, pady=(8, 16))
    for col_idx in range(5):
        qr_frame.columnconfigure(col_idx, weight=1)

    if not hasattr(app, "_about_images"):
        app._about_images = {}
    if not hasattr(app, "_about_raw_data"):
        app._about_raw_data = {}

    root = Path(getattr(app, "project_root", Path.cwd()))
    base_dir = root / "backend-web" / "static" / "qrcode"

    for col_idx, (filename, label_text) in enumerate(_QRCODE_ITEMS):
        img_path = base_dir / filename
        _create_qrcode_cell(app, qr_frame, col_idx, img_path, label_text)


def _create_qrcode_cell(app, parent, col, img_path, label_text):
    cell = tk.Frame(parent, bg=COLORS["card_bg"])
    cell.grid(row=0, column=col, padx=6, pady=4, sticky="nsew")

    tk.Label(cell, text=label_text, font=("微软雅黑", 9, "bold"),
             fg=COLORS["text"], bg=COLORS["card_bg"]).pack(pady=(0, 4))

    img_frame = tk.Frame(cell, width=120, height=120,
                         bg=COLORS["input_bg"],
                         highlightbackground=COLORS["border"],
                         highlightthickness=1)
    img_frame.pack()
    img_frame.pack_propagate(False)

    loading_label = tk.Label(img_frame, text="加载中...",
                             font=("微软雅黑", 8),
                             fg=COLORS["text_secondary"],
                             bg=COLORS["input_bg"])
    loading_label.pack(expand=True)

    threading.Thread(
        target=_load_qrcode_image,
        args=(app, img_frame, loading_label, img_path, label_text),
        daemon=True,
    ).start()


def _load_qrcode_image(app, img_frame, loading_label, img_path, key):
    try:
        with open(img_path, "rb") as f:
            img_data = f.read()
        app.root.after(0, lambda: _display_image(app, img_frame, loading_label, img_data, key))
    except Exception:
        app.root.after(0, lambda: _show_no_image(img_frame, loading_label))


def _display_image(app, img_frame, loading_label, img_data, key):
    try:
        from PIL import Image, ImageTk

        img = Image.open(io.BytesIO(img_data))
        img = img.resize((116, 116), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
    except ImportError:
        try:
            photo = tk.PhotoImage(data=img_data)
        except Exception:
            _show_no_image(img_frame, loading_label)
            return
    except Exception:
        _show_no_image(img_frame, loading_label)
        return

    app._about_images[key] = photo
    app._about_raw_data[key] = img_data

    try:
        loading_label.destroy()
    except tk.TclError:
        return

    try:
        img_label = tk.Label(img_frame, image=photo, bg=COLORS["input_bg"], cursor="hand2")
        img_label.pack(expand=True)
    except tk.TclError:
        pass


def _show_no_image(img_frame, loading_label):
    try:
        loading_label.configure(text="暂无图片")
    except tk.TclError:
        return
    try:
        img_frame.configure(bg=COLORS["input_bg"])
    except tk.TclError:
        pass
