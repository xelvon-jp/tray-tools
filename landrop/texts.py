"""テキスト共有。iPhone から届いたテキストの履歴と、受信時の動作（PCのクリップボードへ・通知）。"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

log = logging.getLogger("landrop")

MAX_TEXT_BYTES = 1024 * 1024
HISTORY_SIZE = 10
_URL = re.compile(r"^https?://\S+$")


@dataclass
class ReceivedText:
    text: str
    client: str
    at: datetime = field(default_factory=datetime.now)

    @property
    def url(self) -> str | None:
        """全体が1つの URL ならそれを返す（共有シートから URL を送った場合）。"""
        s = self.text.strip()
        return s if _URL.match(s) else None

    def preview(self, width: int = 40) -> str:
        one_line = " ".join(self.text.split())
        return one_line if len(one_line) <= width else one_line[: width - 1] + "…"


TextListener = Callable[[ReceivedText], None]


class TextHub:
    """受信したテキストを履歴に積み、リスナーに配る（履歴はメモリのみ。トレイの「最近のテキスト」用）。"""

    def __init__(self) -> None:
        self.history: deque[ReceivedText] = deque(maxlen=HISTORY_SIZE)
        self.listeners: list[TextListener] = []
        self._lock = threading.Lock()

    def receive(self, item: ReceivedText) -> None:
        with self._lock:
            self.history.appendleft(item)
        log.info("テキスト受信: %s from %s", item.preview(), item.client)
        for listener in self.listeners:
            try:
                listener(item)
            except Exception:
                log.exception("テキスト受信後の動作に失敗しました: %s", listener)

    def recent(self) -> list[ReceivedText]:
        with self._lock:
            return list(self.history)


class TextToClipboard:
    """受信したテキストを PC のクリップボードへ入れ、通知する。URL なら通知に「ブラウザで開く」ボタン。"""

    def __init__(self, notify: bool = True, set_text: Callable[[str], None] | None = None) -> None:
        if set_text is None:
            from .clipboard import set_text
        self._set_text = set_text
        self._toast = None
        if notify:
            from .toast import toast  # 通知は別プロセスで出す（常駐の中で WinRT を触ると落ちることがある）

            self._toast = toast

    def __call__(self, item: ReceivedText) -> None:
        self._set_text(item.text)
        if not self._toast:
            return
        title = "LAN Drop: テキストを受信（コピー済み）"
        if url := item.url:
            button = {"activationType": "protocol", "arguments": url, "content": "ブラウザで開く"}
            self._toast(title, item.preview(120), on_click=url, buttons=[button])
        else:
            # win11toast の既定は本文クリックで "http:" を開いてしまうので、クリックしても何もしない形にする
            self._toast(title, item.preview(120), on_click=None, xml=_PLAIN_TOAST_XML)


_PLAIN_TOAST_XML = """
<toast activationType="foreground" scenario="{scenario}">
    <visual>
        <binding template='ToastGeneric'></binding>
    </visual>
</toast>
"""


class TextPopup:
    """受信したテキストを PC の画面右下の小さな窓でプレビューする（コピーはボタンを押したときだけ）。"""

    def __init__(self, launcher=None) -> None:
        self._launcher = launcher

    def __call__(self, item: ReceivedText) -> None:
        from .text_popup import open_popup

        open_popup(item.text, item.client, launcher=self._launcher)


def build_texts(settings) -> TextHub:
    hub = TextHub()
    if settings.text_popup:
        hub.listeners.append(TextPopup())
    if settings.text_to_clipboard:
        hub.listeners.append(TextToClipboard(notify=settings.notify and not settings.text_popup))
    return hub
