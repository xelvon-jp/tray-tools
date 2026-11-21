# -*- coding: utf-8 -*-
"""設定の読み書き（GUI・Qt・COM に依存しない）。"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_DIR / "settings.json"

DEFAULTS: dict = {
    "out_dir": "out",              # 出力先（相対ならプロジェクト基準）
    "model_dir": "models",         # モデル置き場（相対ならプロジェクト基準）
    "final_model": "small",        # 清書用: small / medium / large-v3 など
    "live_model": "base",          # 逐次認識用（軽いモデル）
    "language": "ja",
    "initial_prompt": "",          # 文体の見本など。最初の窓にしか効かない
    "hotwords": "",                # 専門用語（読点区切り）。全窓に効く。清書・逐次の両方に渡す
    "input_device": None,          # None=既定 / 数値=デバイス番号 / 文字列=名前の一部
    "live_enabled": True,          # 逐次認識 ON/OFF
    "live_lines": 4,               # 画面に出す直近の行数
    "compute_type": "int8",
    "device": "cpu",
    "live_cpu_threads": 2,         # 逐次認識が使う CPU スレッド数（録音を邪魔しないため少なめ）
    "final_cpu_threads": 0,        # 0=自動
    "vad_min_rms": 250,            # 発話判定の下限（int16 の RMS）
    "vad_silence_sec": 0.7,        # この秒数の無音で区切る
    "vad_max_segment_sec": 25.0,   # 無音が来なくてもここで強制的に区切る
    "live_max_backlog": 20,        # 逐次認識の待ち行列がこれを超えたら古い区切りを捨てる
    # ---- v2 ----
    "row_sec": 30,                 # タイムラインの1行の秒数
    "silence_stop_sec": 180,       # この秒数の無音が続いたら自動停止（0=無効）
    "silence_stop_peak_dbfs": -50.0,  # これ未満のピークが続けば「無音」とみなす
    "model_idle_release_sec": 600, # モデルをメモリに保持する、最後の使用からの秒数（0=すぐ解放）
    "auto_copy": True,             # 清書完了時にクリップボードへ全文をコピー
    "hotkey_record": "",           # 例 "Ctrl+Alt+F9"（空=未割り当て）。常用アプリと衝突しないこと
    "hotkey_show": "",             # 窓の表示/非表示
    "mic_low_peak_dbfs": -30.0,    # ピークがこれ未満が続くと「小さすぎ」
    "mic_clip_ratio": 0.001,       # ±満杯サンプルの割合がこれを超えると「音割れ」
    "mic_noise_floor_dbfs": -45.0, # 無音時の床がこれを超えると「ノイズ大」
    "mic_dead_peak_dbfs": -70.0,   # ピークがこれ未満なら「無音（デバイス不調/ミュート）」
    "mic_window_sec": 8.0,         # 常時監視の判定窓
    "selected_preset": "",         # Copilot プリセットの最後の選択
    # ---- v2 フェーズ1.5 ----
    "idle_monitor": True,          # 窓が見えている間だけ、待機中もマイクを開いてメーター/診断を動かす（保存も認識もしない）
    "idle_monitor_timeout_sec": 300,  # 操作が無いままこの秒数たったら待機中モニターを閉じる（0=閉じない）
    "idle_prompt_enabled": True,   # 録音していないのに話しているとき「録音していません」を出す
    "idle_prompt_speech_sec": 1.5, # 発話らしい音がこの秒数続いたら指摘
    "idle_prompt_cooldown_sec": 60,  # 指摘を出したあと、この秒数は再度出さない
    "hotkey_pause": "",            # 録音の一時停止/再開（空=未割り当て）
    "paragraph_enabled": True,     # 清書の出力（txt/md/クリップボード）を、すき間の長さで改行する
    "paragraph_gap_sec": 2.0,      # このすき間以上で、直前が句点・？・！なら改行
    "paragraph_force_gap_sec": 3.0,  # このすき間以上なら必ず改行（句点がなければ「。」を補う）
    "compact_mode": False,         # コンパクト表示で起動する
    "always_on_top": False,        # 最前面に固定
    "win_geometry_normal": [],     # [x, y, w, h]（通常表示の位置とサイズ）
    "win_geometry_compact": [],    # [x, y, w, h]（コンパクト表示）
    "display_size": "small",       # 表示サイズ: small / standard / large（設定で切り替え。再起動不要）
    "playback_device": "",         # 履歴の再生出力先の id（空=システム既定。既定が切り替わったら追従）
}


def save_settings(data: dict, path: Path | None = None) -> None:
    """設定を書き戻す（UTF-8 BOMなし・LF）。"""
    path = path or SETTINGS_PATH
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def load_settings(path: Path | None = None, create: bool = True) -> dict:
    path = path or SETTINGS_PATH
    data = dict(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(user, dict):
                data.update(user)
        except Exception as e:  # 壊れていても既定で動く
            print(f"[settings] 読み込み失敗、既定値を使います: {e}")
    elif create:
        try:
            path.write_text(json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return data


def resolve_dir(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else PROJECT_DIR / p
