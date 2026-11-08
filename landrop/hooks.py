"""受信後処理の差し込み口。

  - transform フック：保存直後・レスポンス前に1ファイルずつ。ファイルを差し替え得る（例：HEIC→JPEG）
  - listener フック ：1リクエスト分の受信が終わった後にバックグラウンドで（例：Windows通知）

どれを有効にするかは build_hooks() が設定から決める。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger("landrop")


@dataclass
class ReceivedFile:
    path: Path
    original_name: str
    size: int
    client: str
    batch: str = ""  # 何回目の受信か（SessionDirs のバッチ id）。同じ回のものをまとめて扱うため
    duplicate: bool = False  # 同じ日に同じファイルが既にあり、新しくは保存しなかった（path は既存のもの）


# 1ファイルごと。返り値で ReceivedFile を差し替えられる（例：変換後のJPEGパス）
TransformHook = Callable[[ReceivedFile], ReceivedFile]
# 1リクエスト分の受信完了後
ListenerHook = Callable[[list[ReceivedFile]], None]


class HookRegistry:
    def __init__(self) -> None:
        self.transforms: list[TransformHook] = []
        self.listeners: list[ListenerHook] = []

    def apply_transforms(self, item: ReceivedFile) -> ReceivedFile:
        for hook in self.transforms:
            try:
                item = hook(item)
            except Exception:
                # 後処理の失敗で受信自体は失敗させない（元ファイルは保存済み）
                log.exception("transform hook failed: %s", hook)
        return item

    def notify(self, items: list[ReceivedFile]) -> None:
        for hook in self.listeners:
            try:
                hook(items)
            except Exception:
                log.exception("listener hook failed: %s", hook)


def log_received(items: list[ReceivedFile]) -> None:
    for it in items:
        note = "（同じファイルが既にあるので保存せず、履歴だけ記録）" if it.duplicate else ""
        log.info("受信: %s (%s) from %s%s", it.path.name, human_size(it.size), it.client, note)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def build_hooks(settings: "Settings") -> HookRegistry:
    """設定に応じてフックを組み立てる。オプション機能の準備に失敗してもサーバーは起動させる。"""
    hooks = HookRegistry()
    hooks.listeners.append(log_received)

    if settings.heic_to_jpeg:
        try:
            from .heic import HeicToJpeg

            hooks.transforms.append(HeicToJpeg(settings.jpeg_quality, settings.heic_keep_original))
        except Exception:
            log.exception("HEIC→JPEG変換を有効にできませんでした（pillow-heif 未インストール？）")

    # 何をするかは AfterBurst が実行時に settings を見て決める（トレイのチェックで切り替わるため常に登録）
    from .notify import AfterBurst, Debounced

    hooks.listeners.append(Debounced([AfterBurst(settings)]))

    return hooks
