# -*- coding: utf-8 -*-
"""認識モデルの候補一覧と、取得済みの判定・取得（Qt 非依存）。

- CATALOG        : 画面に出す候補（faster-whisper 形式）。設定にカタログ外の名前が入っていてもそのまま使える。
- is_downloaded  : 取得済みか。ネットに出ない。load_model（transcriber）と同じ規則（model_cache.find_local_model）。
- download       : 利用者が［取得］を押したときだけ呼ぶ。進み具合をコールバックで通知し、失敗は日本語の理由で返す。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from model_cache import find_local_model, repo_id_for


@dataclass(frozen=True)
class ModelInfo:
    id: str            # 画面・選択肢の識別子（カタログ内で一意）
    title: str         # 画面の名前
    model: str         # faster-whisper に渡す名前（短い名前、または 'org/repo'）。settings にはこれを保存する
    size_gb: float     # おおよその大きさ
    speed: str         # 速さの一言
    accuracy: str      # 精度の一言
    note: str = ""     # 注意

    @property
    def repo(self) -> str:
        return repo_id_for(self.model)


CATALOG: tuple[ModelInfo, ...] = (
    ModelInfo("tiny", "tiny", "tiny", 0.08, "最速", "低", "仮認識向け。清書には粗いです"),
    ModelInfo("base", "base", "base", 0.15, "速い", "やや低"),
    ModelInfo("small", "small", "small", 0.5, "やや速い", "中"),
    ModelInfo("medium", "medium", "medium", 1.5, "遅め", "高"),
    ModelInfo("large-v3-turbo", "large-v3-turbo", "large-v3-turbo", 1.6, "中", "高",
              "large-v3 を軽くしたモデル。多くの場合 medium 以上の精度で、large-v3 より速いです"),
    ModelInfo("large-v3", "large-v3", "large-v3", 3.1, "遅い", "最高", "メモリを多く使います（約3GB以上）"),
    ModelInfo("kotoba-whisper", "kotoba-whisper（日本語特化）", "kotoba-tech/kotoba-whisper-v2.0-faster",
              1.5, "遅め", "高（日本語）", "日本語専用。英語などの混ざる録音には向きません。このPCでは、短い音でも1回9秒ほどかかる（実測。medium と同じ）"),
)

_BY_KEY: dict[str, ModelInfo] = {}
for _m in CATALOG:
    _BY_KEY[_m.id] = _m
    _BY_KEY.setdefault(_m.model, _m)


def find(key: str) -> ModelInfo | None:
    """id か faster-whisper に渡す名前から候補を引く。カタログ外なら None。"""
    return _BY_KEY.get((key or "").strip())


def model_name(key: str) -> str:
    """settings に保存する名前（カタログの id なら対応するモデル名、カタログ外ならそのまま）。"""
    m = find(key)
    return m.model if m else (key or "").strip()


def is_downloaded(key: str, model_dir) -> bool:
    """取得済みか（ネットに出ない）。カタログ外の名前でも、同じ規則で調べる。"""
    name = model_name(key)
    if not name:
        return False
    return find_local_model(name, Path(model_dir)) is not None


def state_label(key: str, model_dir) -> str:
    return "【取得済み】" if is_downloaded(key, model_dir) else "【未取得】"


# 用途ごとの「非推奨」。AI対話の認識は、発話のたびに動くので、速さが要る（medium 以上は、短い発話でも、1回9秒以上かかる実測）。
# 録音全体の清書は、裏で動くので、遅くてよいが、精度の低いモデルは向かない
DISCOURAGED = {
    "dialogue": {"tiny": "精度が低い", "base": "精度が低い", "medium": "遅い（1発話ごとに9秒以上）",
                 "large-v3-turbo": "遅い", "large-v3": "遅い", "kotoba-tech/kotoba-whisper-v2.0-faster": "遅い"},
    "final": {"tiny": "精度が低い", "base": "精度が低い"},
}


def supports_hotwords(key: str) -> bool:
    """専門用語（hotwords）を渡してよいモデルか。kotoba-whisper は、hotwords を渡すと、何も出力しなくなる（実測 2026-11-23:
    同じ音声で、hotwords ありは0件、なしは全文）ので、渡さない。"""
    return "kotoba" not in (key or "").lower()


def discouraged_reason(key: str, usage: str | None) -> str:
    """その用途（"dialogue"=AI対話の認識／"final"=録音全体の清書）で非推奨なら、理由。そうでなければ空。"""
    return (DISCOURAGED.get(usage or "") or {}).get(model_name(key), "")


def display_label(key: str, model_dir, usage: str | None = None) -> str:
    """選択肢の表示。例: 'large-v3-turbo — 精度:高 速さ:中 約1.6GB【取得済み】'。カタログ外は名前だけ。
    usage を渡すと、その用途で非推奨のものに「【非推奨：理由】」を足す。"""
    m = find(key)
    st = state_label(key, model_dir)
    bad = discouraged_reason(key, usage)
    mark = f"【非推奨：{bad}】" if bad else ""
    if m is None:
        return f"{(key or '').strip()} — （一覧にないモデル）{st}{mark}"
    return f"{m.title} — 精度:{m.accuracy} 速さ:{m.speed} 約{m.size_gb:g}GB{st}{mark}"


def combo_items(model_dir, current: str = "", usage: str | None = None) -> list[tuple[str, str]]:
    """コンボボックス用の (表示, 保存する名前) の一覧。current がカタログ外なら末尾に足して、そのまま選べるようにする。
    usage（"dialogue"／"final"）を渡すと、その用途で非推奨のものに印を付ける。"""
    items = [(display_label(m.model, model_dir, usage), m.model) for m in CATALOG]
    cur = model_name(current)
    if cur and all(cur != v for _, v in items):
        items.append((display_label(cur, model_dir, usage), cur))
    return items


# ---------------------------------------------------------------- 取得
class DownloadCancelled(Exception):
    pass


@dataclass
class DownloadResult:
    ok: bool
    message: str = ""
    path: str = ""
    cancelled: bool = False


_ALLOW = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def failure_message(key: str, model_dir, cause: str) -> str:
    """取得に失敗したときの日本語の理由＋手置きの手順への案内。"""
    from transcriber import offline_help
    return offline_help(model_name(key), Path(model_dir), cause)


def _real_snapshot(repo: str, model_dir: str, progress_cb, cancel):
    """huggingface_hub で model_dir（HF キャッシュ形式）へ取得。バイト数で進み具合を通知する。"""
    import io
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import tqdm as hf_tqdm

    lock = threading.Lock()
    bars: dict[int, list[int]] = {}      # バーごとの [n, total]（unit が B のものだけ＝ファイルのバイト数）

    def report() -> None:
        with lock:
            done = max((b[0] for b in bars.values()), default=0)
            total = max((b[1] for b in bars.values()), default=0)
        progress_cb(done, max(total, done))

    class _Bar(hf_tqdm):
        def __init__(self, *a, **k):
            k["disable"] = False
            k["file"] = io.StringIO()            # 端末には何も出さない
            super().__init__(*a, **k)
            self._tracked = (getattr(self, "unit", "") == "B")

        def update(self, n=1):
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled()
            r = super().update(n)
            if self._tracked:
                with lock:
                    bars[id(self)] = [int(self.n or 0), int(self.total or 0)]
                report()
            return r

    return snapshot_download(repo, cache_dir=model_dir, allow_patterns=_ALLOW, tqdm_class=_Bar)


def download(key: str, model_dir, progress_cb: Callable[[int, int], None] = lambda done, total: None,
             cancel: threading.Event | None = None, snapshot: Callable | None = None) -> DownloadResult:
    """model_dir へ取得する（利用者が［取得］を押したときだけ呼ぶこと）。

    progress_cb(取得したバイト数, 全体のバイト数)。cancel がセットされたら中断。
    snapshot は差し替え用（テストは偽物を渡す）。(repo, model_dir, progress_cb, cancel) -> 保存先パス。
    例外は投げず DownloadResult で返す。"""
    name = model_name(key)
    if not name:
        return DownloadResult(False, "モデル名が空です")
    model_dir = Path(model_dir)
    try:
        model_dir.mkdir(parents=True, exist_ok=True)
        path = (snapshot or _real_snapshot)(repo_id_for(name), str(model_dir), progress_cb, cancel)
    except DownloadCancelled:
        return DownloadResult(False, "取得をキャンセルしました（途中まで取得した分は、次回の取得で続きから再開します）", cancelled=True)
    except Exception as e:
        if cancel is not None and cancel.is_set():
            return DownloadResult(False, "取得をキャンセルしました", cancelled=True)
        return DownloadResult(False, failure_message(name, model_dir, f"{type(e).__name__}: {e}"))
    if not is_downloaded(name, model_dir):
        return DownloadResult(False, failure_message(name, model_dir, "取得は終わりましたが、model.bin が見つかりません"))
    return DownloadResult(True, "取得しました", str(path))
