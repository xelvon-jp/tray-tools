"""受信が一段落したときの動作（listener フック）：通知・受信フォルダを開く・画像1枚ならクリップボードへ。"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .config import Settings
from .hooks import ReceivedFile, human_size

log = logging.getLogger("landrop")

BurstAction = Callable[[list[ReceivedFile]], None]


class Debounced:
    """受信が一段落したら、それまでの分をまとめて actions に渡す。

    Safari のページは1ファイル1リクエストで送るので、リクエストごとに動くと
    10枚送れば通知が10通になる。最後の受信から `quiet_sec` 秒あくまで待ってまとめる。
    """

    def __init__(self, actions: list[BurstAction], quiet_sec: float = 2.0) -> None:
        self.actions = actions
        self.quiet_sec = quiet_sec
        self._pending: list[ReceivedFile] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def __call__(self, items: list[ReceivedFile]) -> None:
        with self._lock:
            self._pending.extend(items)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.quiet_sec, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            items, self._pending, self._timer = self._pending, [], None
        if not items:
            return
        for action in self.actions:
            try:
                action(items)
            except Exception:
                log.exception("受信後の動作に失敗しました: %s", action)


def received_folder(items: list[ReceivedFile]) -> Path:
    """まとめて受け取った中で最後のファイルのフォルダ（日付フォルダ）。"""
    return items[-1].path.parent


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif"}


def single_image(items: list[ReceivedFile]) -> ReceivedFile | None:
    """ひとまとまりの受信が画像1枚だけならそれを返す。"""
    if len(items) == 1 and items[0].path.suffix.lower() in IMAGE_EXTS:
        return items[0]
    return None


class AfterBurst:
    """受信が一段落したときの動作をまとめて行う。

    - 画像1枚だけなら：クリップボードへ（single_image_clipboard）／フォルダの代わりに画像を開く（single_image_open）
    - それ以外：受信フォルダを Explorer で開く（open_folder。同じフォルダは1回だけ）
    - 通知（notify。クリックで受信フォルダ）

    設定はその都度 settings から読む（トレイのチェックで実行中に切り替わるため）。
    """

    def __init__(
        self,
        settings: Settings,
        opener: Callable[[Path], None] | None = None,
        set_image: Callable[[Path], None] | None = None,
        toast: Callable[..., object] | None = None,
        reveal: Callable[[Path], None] | None = None,
    ) -> None:
        self.settings = settings
        self._open = opener or (lambda p: os.startfile(p))
        if set_image is None:
            from .clipboard import set_image
        self._set_image = set_image
        self._toast = toast if toast is not None else _toast_in_subprocess
        self._reveal = reveal or reveal_in_explorer
        self._opened: set[str] = set()

    def __call__(self, items: list[ReceivedFile]) -> None:
        s = self.settings
        image = single_image(items)
        copied = False
        if image and s.single_image_clipboard:
            try:
                self._set_image(image.path)
                copied = True
            except Exception:
                log.exception("画像をクリップボードに入れられませんでした: %s", image.path)

        if image and s.single_image_open:
            self._open(image.path)
        elif s.open_folder:
            self._reveal_once(items)

        if s.notify and self._toast:
            title, body = format_message(items, copied=copied)
            # app_id は既定（"Python"）のまま。未登録の独自IDだと Windows が通知を出さないため
            self._toast(title, body, on_click=received_folder(items).resolve().as_uri())

    def _reveal_once(self, items: list[ReceivedFile]) -> None:
        """受信フォルダを開き、今回の1枚目を選んだ状態にする。

        日付フォルダには同じ日の他の受信も入っているので、どこからが今回の分か分かるように選択する。
        同じ回（バッチ）は1回だけ（追加で届くたびに窓が増えないように）。
        """
        key = items[0].batch or str(received_folder(items))
        if key in self._opened:
            return
        self._opened.add(key)
        self._reveal(items[0].path.resolve())


def reveal_in_explorer(path: Path) -> None:
    # explorer は成功しても終了コード 1 を返すことがあるので結果は見ない
    subprocess.Popen(["explorer", f"/select,{path}"], close_fds=True)


def _toast_in_subprocess(title: str, body: str, **kwargs) -> None:
    # 常駐プロセスの中で WinRT を触ると落ちることがあるので、通知は別プロセスで出す（toast.py 参照）
    from .toast import toast

    toast(title, body, **kwargs)


def format_message(items: list[ReceivedFile], copied: bool = False) -> tuple[str, str]:
    total = sum(it.size for it in items)
    if len(items) == 1:
        title = "LAN Drop: 画像を受信（コピー済み）" if copied else "LAN Drop: 受信しました"
        return title, f"{items[0].path.name}（{human_size(total)}）"
    names = "、".join(it.path.name for it in items[:3])
    more = f" ほか{len(items) - 3}件" if len(items) > 3 else ""
    return f"LAN Drop: {len(items)}件 受信しました", f"{names}{more}（計 {human_size(total)}）"
