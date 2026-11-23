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
    # （待機中のマイク確認を止めるまでの無操作時間 300 秒は、ui_main.IDLE_MONITOR_TIMEOUT_SEC の固定値）
    "idle_prompt_enabled": True,   # 録音していないのに話しているとき「録音していません」を出す（設定画面には出さない固定値。session.poll_idle_prompt が読む）
    "idle_prompt_speech_sec": 1.5, # 発話らしい音がこの秒数続いたら指摘
    "idle_prompt_cooldown_sec": 60,  # 指摘を出したあと、この秒数は再度出さない
    "hotkey_pause": "",            # 録音の一時停止/再開（空=未割り当て）
    "paragraph_enabled": True,     # 清書の出力（txt/md/クリップボード）を、すき間の長さで改行する
    "paragraph_gap_sec": 2.0,      # このすき間以上で、直前が句点・？・！なら改行
    "paragraph_force_gap_sec": 3.0,  # このすき間以上なら必ず改行（句点がなければ「。」を補う）
    "always_on_top": False,        # 最前面に固定
    "display_size": "small",       # 表示サイズ: small / standard / large（設定で切り替え。再起動不要）
    "audio_compress": "delete",    # 清書後に録音 WAV を Opus へ圧縮: off(しない) / keep(WAVも残す) / trash(WAVはごみ箱へ) / delete(WAVは完全削除。Opus への変換に成功したときだけ)
    "audio_compress_kbps": 24,     # Opus のビットレート（kbps、モノラル）
    "audio_silence_cut": True,     # Opus にするとき、4秒以上の無音を0.5秒に縮める（縮めた場所は履歴に記録し、再生位置を合わせる）
    "playback_device": "",         # 履歴の再生出力先の id（空=システム既定。既定が切り替わったら追従）
    # ---- 統合 GUI（3行窓＋引き出し）。従来の左右2ペイン（ui_layout / win_geometry_normal 等）は廃止。古い設定ファイルに残っていても無視する
    "win_geometry_unified": [],    # [x, y, w, h]（引き出しを閉じているとき）
    "win_geometry_unified_drawer": [],  # [x, y, w, h]（統合 GUI・引き出しを開いているとき）
    "unified_drawer_open": False,  # 引き出しを開いて起動する
    "unified_drawer_tab": 0,       # 引き出しの最後のタブ（0=履歴 1=用語・置換 2=Copilot 3=設定）
    "unified_expanded": False,     # 下書きを広げた状態（▾）で起動する
    "finalize_on_end": True,       # セッション終了時に、録音全体を清書（録音全体の清書モデル final_model）して履歴に保存する
}

# ---- AI対話モード（段階A）。settings.json の "dialogue" 節。部分的に書かれていても足りないキーは既定で補う
# （AI対話モードは常に有効。古い設定ファイルに "enabled" が残っていても無視する。仮認識・壁打ち・クリップボードコピーは廃止:
#  provisional / provisional_interval_sec / provisional_wait_sec / dest_clipboard / unified_mode / auto_copy / live_model などが
#  残っていても、読み込みで無視する）
DIALOGUE_DEFAULTS: dict = {
    "worker_python": "",           # UIAワーカーを動かすpython（空=既定の場所。実パスは書かない）
    "utterance_gap_sec": 1.2,      # この秒数の無音で発話を区切って認識する
    # 無音による自動確定（auto_commit / message_gap_sec）は採用しない: 下書きの内容を、利用者が何も操作していないのに
    # 宛先へ入れてはならない（確定はボタン・ヘッドセット・ショートカットのときだけ）。設定ファイルに書かれていても使われない
    "headset_button": False,       # ヘッドセットの多機能ボタン（単押し）を確定に使う
    "headset_mode": "auto",        # 検出経路: auto（HID直接読み＋ホットキー独占を併用）/ hid（HIDだけ）/ hotkey（ホットキーだけ）
    "headset_vid_pid": "3511:2EF2", # HID 直接読みで探すヘッドセットの ID（VID:PID、16進）
    "mouse_button": "",            # マウスのサイドボタンを確定に使う: ""（なし）/ "back"（戻る）/ "forward"（進む）。セッション中だけ低レベルフックで拾い、そのボタンは他のアプリへ渡さない（headset_button とは独立）
    "click_commit": False,         # 【実験】舌打ちを2回続けて鳴らすと確定（既定OFF）。マイクの音だけで判定（追加ソフト・権限は不要）。セッション中だけ。静かな場所向け。詳細は dialogue_click.py
    "existing_text": "append",     # 入力欄に書きかけがあるとき: append / overwrite / abort
    "keep_idle_foreground": False, # 入力中の他作業を奪わない（直近キー入力が quiet_input_sec 以内なら前面化しない）
    "return_focus": False,         # 配達のあと元の窓へ戻す
    "quiet_input_sec": 3,          # 上の「直近のキー入力」とみなす秒数
    "last_target": "",             # 最後に使った宛先プリセット名
    "model": "small",              # 区切りごとの認識モデル
    "min_utterance_sec": 0.3,      # これより短い音（クリック音など）は発話とみなさない
    "voice_min_dbfs": -50.0,       # 認識開始ライン: 音量バーと同じ基準（0.1秒の平均＝RMS）。これに届く音が一瞬もない塊は、波形・仮認識・清書へ回さない（null で無効）
    "max_utterance_sec": 30.0,     # 無音が来なくてもここで強制的に区切る
    "idle_stop_sec": 180,          # 無音がこの秒数続いたらセッションを自動終了（0=しない）
    "merge_undelivered": False,    # 未配達が複数あるとき、まとめて1通で再試行する
    "context_sentences": 2,        # 認識の文脈に渡す直前の発話数
    "send_after_deliver": False,   # 入力欄へ届けたあと、送信（送信ボタン／Enter）まで行う
    "wait_while_busy": True,       # 送信まで行うとき、相手が回答中（停止ボタンが見える）なら待つ
    "busy_wait_sec": 120,          # 回答中の待ちの上限（超えたら送らず「回答中のため未送信」）
    "newline_after_sentence": True,  # 文中の「。」「？」「！」の直後に改行を入れる
    "break_blank_line": True,      # 無音で区切った所（前が句点系で終わっているとき）は改行2つ（空行）で区切る
    "punctuate_on_commit": True,   # 確定のとき、下書きの末尾が句読点で終わっていなければ「。」を補う
    "dest_record_only": False,     # 宛先「なし（記録だけ）」: 何も届けず・クリップボードにも入れず、確定済みとして記録に残すだけにする
}
DEFAULTS["dialogue"] = dict(DIALOGUE_DEFAULTS)


def dialogue_config(settings: dict | None) -> dict:
    """settings の dialogue 節を既定で補って返す（元の dict は変えない）。"""
    d = dict(DIALOGUE_DEFAULTS)
    user = (settings or {}).get("dialogue")
    if isinstance(user, dict):
        d.update(user)
    return d


def save_settings(data: dict, path: Path | None = None) -> None:
    """設定を書き戻す（UTF-8 BOMなし・LF）。"""
    path = path or SETTINGS_PATH
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def load_settings(path: Path | None = None, create: bool = True) -> dict:
    path = path or SETTINGS_PATH
    data = dict(DEFAULTS)
    data["dialogue"] = dict(DIALOGUE_DEFAULTS)   # 共有の dict を書き換えられないよう複製
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(user, dict):
                data.update(user)
                if isinstance(user.get("dialogue"), dict):   # 節の一部だけ書かれていても、足りないキーを既定で補う
                    data["dialogue"] = {**DIALOGUE_DEFAULTS, **user["dialogue"]}
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
