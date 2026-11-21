# -*- coding: utf-8 -*-
"""再生出力先の一覧と選択のロジック（Qt 非依存。一覧の取得元は差し替えられる）。

- OutputDevice : id（保存する文字列）・表示名・既定か・Qt の実体（本物の取得元だけが持つ）
- DeviceProvider : list() で OutputDevice のリストを返す。本物は QMediaDevices、テストは偽物
- resolve_output(selected_id, devices) : 設定の選択 → 実際に使う出力先
    selected_id が空（システム既定）→ 毎回「いまの既定」を返す（既定が切り替わったら追従するため）
    選んだ出力先が無い → システム既定に戻し、notice に知らせる文言を入れる
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

DEFAULT_LABEL = "システム既定"


@dataclass(frozen=True)
class OutputDevice:
    id: str
    name: str
    is_default: bool = False
    qt: object | None = None  # QAudioDevice（本物のときだけ）


class DeviceProvider(Protocol):
    def list(self) -> list[OutputDevice]: ...


@dataclass
class Resolution:
    device: OutputDevice | None   # None = 出力先が1つも無い
    follows_default: bool         # システム既定に追従しているか
    notice: str = ""              # 選んだ出力先が無くなって既定に戻したときの知らせ
    reset_setting: bool = False   # 設定の playback_device を空に戻すべきか


def default_device(devices: list[OutputDevice]) -> OutputDevice | None:
    for d in devices:
        if d.is_default:
            return d
    return devices[0] if devices else None


def resolve_output(selected_id: str, devices: list[OutputDevice]) -> Resolution:
    sel = (selected_id or "").strip()
    if not sel:
        return Resolution(default_device(devices), True)
    for d in devices:
        if d.id == sel:
            return Resolution(d, False)
    return Resolution(default_device(devices), True,
                      notice="選んだ出力先が見つからないため、システム既定に戻しました",
                      reset_setting=True)


def combo_entries(devices: list[OutputDevice]) -> list[tuple[str, str]]:
    """コンボボックス用の (表示, id)。先頭は「システム既定」（id は空）。既定のデバイスには印を付ける。"""
    out = [(DEFAULT_LABEL, "")]
    for d in devices:
        out.append((d.name + ("（現在の既定）" if d.is_default else ""), d.id))
    return out


class QtDeviceProvider:
    """QMediaDevices から出力先の一覧を取る（取得だけ。音は出さない）。"""

    def list(self) -> list[OutputDevice]:
        from PySide6.QtMultimedia import QMediaDevices
        out = []
        for d in QMediaDevices.audioOutputs():
            out.append(OutputDevice(bytes(d.id()).decode("utf-8", "replace"), d.description(),
                                    bool(d.isDefault()), d))
        return out
