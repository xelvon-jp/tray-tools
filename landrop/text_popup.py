"""iPhone から届いたテキストを PC の画面右下に小さな窓で見せる（別プロセスで起動）。

クリップボードは勝手に書き換えない。「クリップボードにコピー」を押したときだけコピーする。
Windows の通知（トースト）はボタンを押したときに処理を走らせる手段が無いので、自前の窓にした。
常駐とは別プロセスなので、窓が開いている間も受信は止まらず、窓が落ちても常駐は巻き込まれない。

    python -m landrop.text_popup '{"text": "...", "client": "192.168.1.52"}'

キー操作：Enter＝コピー（ボタンにフォーカスがあればそのボタン）、Esc＝閉じる、Tab／←→＝ボタン移動
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import webbrowser
from typing import Callable

from .config import PROJECT_ROOT

_URL = re.compile(r"^https?://\S+$")


def open_popup(text: str, client: str = "", launcher: Callable[[list[str]], object] | None = None) -> None:
    """常駐側から呼ぶ。待たずに戻る。"""
    payload = json.dumps({"text": text, "client": client}, ensure_ascii=False)
    cmd = [sys.executable, "-m", "landrop.text_popup", payload]
    if launcher:
        launcher(cmd)
        return
    subprocess.Popen(
        cmd, cwd=PROJECT_ROOT, creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )


def show(text: str, client: str = "") -> None:
    import tkinter as tk
    from tkinter import ttk

    from .clipboard import set_text

    url = text.strip() if _URL.match(text.strip()) else None

    root = tk.Tk()
    root.title("LAN Drop：テキストを受信")
    root.attributes("-topmost", True)  # 他の窓の裏に隠れないように（キーボードの入力先は奪わない）
    root.resizable(True, True)
    root.minsize(360, 180)

    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=f"iPhone から届いたテキスト{f'（{client}）' if client else ''}").pack(anchor="w")

    lines = min(12, max(3, text.count("\n") + 1 + len(text) // 60))
    box = tk.Text(frame, wrap="char", height=lines, width=56, font=("Yu Gothic UI", 10), relief="solid", borderwidth=1)
    box.insert("1.0", text)
    box.configure(state="disabled")  # 読み取り専用（選択して一部だけコピーするのはできる）
    box.pack(fill="both", expand=True, pady=(6, 10))

    status = ttk.Label(frame, text="")
    status.pack(anchor="w")

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(6, 0))

    def copy() -> None:
        try:
            set_text(text)
        except OSError:
            status.configure(text="コピーできませんでした（他のアプリがクリップボードを使用中）")
            return
        status.configure(text="クリップボードにコピーしました")
        root.after(700, root.destroy)

    def open_url() -> None:
        webbrowser.open(url)
        root.destroy()

    btns = [ttk.Button(buttons, text="クリップボードにコピー", command=copy)]
    if url:
        btns.append(ttk.Button(buttons, text="ブラウザで開く", command=open_url))
    btns.append(ttk.Button(buttons, text="閉じる", command=root.destroy))
    for b in reversed(btns):
        b.pack(side="right", padx=(6, 0))

    # キー操作：←→ でボタン移動、Enter でフォーカス中のボタン（最初は「コピー」）、Esc で閉じる
    def press_focused(_e=None) -> str:
        w = root.focus_get()
        (w if w in btns else btns[0]).invoke()
        return "break"

    def move(delta: int) -> Callable[[object], str]:
        def handler(_e=None) -> str:
            w = root.focus_get()
            i = btns.index(w) if w in btns else 0
            btns[(i + delta) % len(btns)].focus_set()
            return "break"
        return handler

    root.bind("<Return>", press_focused)
    root.bind("<Escape>", lambda _e: root.destroy())
    for b in btns:
        b.bind("<Left>", move(-1))
        b.bind("<Right>", move(1))

    # 画面右下（タスクバーの上）に出す
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = root.winfo_screenwidth() - w - 24
    y = root.winfo_screenheight() - h - 80
    root.geometry(f"+{max(0, x)}+{max(0, y)}")
    btns[0].focus_set()
    root.mainloop()


if __name__ == "__main__":
    data = json.loads(sys.argv[1])
    show(data.get("text", ""), data.get("client", ""))
