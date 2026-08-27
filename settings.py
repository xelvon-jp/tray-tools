# settings.py
# settings.json の読み書きを行う。存在しない場合は自動生成し、壊れている場合は
# デフォルトにフォールバックする(rapture-py/settings.py の挙動を踏襲)。
import json
import time
from pathlib import Path

# 既定の保存先。ドライブ直下(C:\bak 等)は権限の無いPCでは作成に失敗しうるので、
# 必ず書けるユーザープロファイル配下にする。
DEFAULT_SAVE_FOLDER = str(Path.home() / "Pictures" / "rapture")

# 音声デバイスIDはPCごとに異なるGUIDなので、既定値としては持てない(他PCで使うと
# 存在しないIDになる)。setup.py での検出か list_devices.py の出力から設定する。
DEFAULT_SETTINGS = {
    "capture": {
        "hide_duration_ms": 2000,
        "save_folder": DEFAULT_SAVE_FOLDER,
        "save_format": "png",
        "jpeg_quality": 90,
        "history_days": 0,
        "pen_color": "#ff0000",
        "pen_width": 3,
        "highlighter_enabled": False,
        # 付箋が出るまでの待ちを消すための待機プロセス。有効(既定)にすると、Python と Qt を
        # 読み込んで最初の描画まで済ませた付箋プロセスを常に1つ起こしておき、キャプチャの
        # ときはそこへ画像と表示位置を送るだけにする(範囲を選んでから付箋が出るまで
        # 実測10ms前後)。false にすると1枚ごとにプロセスを起こす従来の形へ戻る
        # (このPCで355ms、遅いPCでは3秒以上かかるという報告もある)。
        # 代償は常時1プロセスぶんのメモリ(専有27MB程度)だけ。有効でも無効でも、
        # キャプチャそのものの成否は変わらない(待機役が居なければ従来の道へ落ちる)。
        "prewarm": True,
    },
    "screen": {
        # スリープ抑止サブメニューに並べる時限(分)。「無期限」と「解除」は常に付く。
        "keep_awake_minutes": [30, 120],
        # マウスジグラーも同じ作り(時限の選択肢＋無期限＋解除)。スリープ抑止と違って
        # 周期的に入力を送るので、送る間隔と「何秒無操作なら送るか」も持つ。
        # 間隔の既定60秒は、リモートデスクトップのアイドルタイムアウト(通常10〜15分)に
        # 対して十分余裕がある値。
        "jiggler_minutes": [30, 120],
        "jiggler_interval_seconds": 60,
        "jiggler_idle_seconds": 30,
    },
    "audio": {
        "devices": [],
    },
    # 外から呼ぶツール。空なら同梱のものを使う(presenter.html はこのフォルダにある)。
    # 別の場所に置いたものを使いたいときだけパスを書く。
    "tools": {
        "presenter": "",
    },
    # フォルダブックマーク。bookmarks は {"name": 表示名, "path": フォルダパス} の配列で、
    # アプリからの登録で増える(削除・並べ替えは settings.json を直接編集する想定)。
    # target は移動先の決め方: auto(呼んだ時の前面がエクスプローラならそこ、でなければ
    # あふｗ) / afxw(常にあふｗ) / explorer(常にエクスプローラ)。
    "launcher": {
        "target": "auto",
        "afxw_path": r"C:\soft\afxw\AFXW.EXE",
        "bookmarks": [],
    },
    # 定型文。recent は最近コピーしたテンプレート名(新しいものが先頭・上限20件)で、
    # ピッカーの並び順に使う。アプリが自分で書き足す値なので手で編集する必要はない。
    "snippets": {
        "recent": [],
    },
    # 各ディスプレイのタスクバーに置く、通知領域の代わりの小さなウィジェット。
    # Windowsは通知領域をプライマリのタスクバーにしか出さないため、正面のモニタを
    # セカンダリにしている構成では、トレイを触るたびに視線と手が別の画面へ行ってしまう。
    # 既定は無効。タスクバーが1つしか無いPC(そのまま設定を持ち回るノート等)で勝手に出さない。
    #
    # all_displays が True ならプライマリを含む全ディスプレイに1つずつ出す。False なら
    # セカンダリだけ。置き先はタスクバーの数ではなく画面の数で決めるので、Windowsの
    # 「タスクバーをすべてのディスプレイに表示する」がオフの環境でも全画面に出る
    # (その画面ではタスクバーがあるはずの位置＝画面下端を基準にする)。
    #
    # positions は位置をディスプレイごとに持つ辞書。キーは QScreen.name()(Windowsなら
    # "\\.\DISPLAY1" 等)、値は {"right": 右端からの距離, "top": 基準の上端からの距離}。
    # Ctrl+左ドラッグで動かしたウィジェットの分だけがここへ書かれる(他の画面は動かない)。
    # 書かれていない画面は既定位置(Windows 11 の時計の実測位置)へ自動配置するので、
    # 別のPCへこのファイルを持って行っても壊れない(知らない画面名は既定に落ちるだけ)。
    #
    # background_color が None なら、表示する直前にその位置の画面を撮って最頻色を使う
    # (タスクバーの透明効果で壁紙が透けるため、決め打ちの色では浮く)。
    # clock_format_* は strftime の書式。先頭ゼロを落とすのは Windows では %#H(%-H はLinux系)。
    #
    # launcher_* は、ウィジェットにマウスを乗せている間だけ真上に出る縦一列のランチャ
    # (taskbar_launcher.py)。本体は時計に化ける都合で幅が59px前後しかなく、Rapture と
    # 音声の2つで埋まる。それ以外の機能へ通知領域まで戻らずに届くようにするためのもの。
    # launcher_items は上から下へ並ぶ順で、書ける名前は
    # capture(キャプチャ) / audio(音声出力切替) / ruler(画面定規) /
    # color_picker(カラーピッカー) / snippets(定型文) / bookmarks(フォルダブックマーク) /
    # presenter(発表者ツール)。
    # 減らしても増やしても順を入れ替えてもよい。[] にするか launcher_enabled を False に
    # すると、マウスを乗せても従来どおり本体だけになる。
    # launcher_close_delay_ms は、カーソルが離れてから畳むまでの猶予。本体からパネルへ
    # 移る途中には必ず「どちらにも乗っていない」瞬間があるので、0にすると項目へ届く前に
    # 閉じる。
    "taskbar_widget": {
        "enabled": False,
        "all_displays": True,
        "positions": {},
        "width": None,
        "height": 31,
        "background_color": None,
        "text_color": None,
        "clock_format_top": "%m/%d(%a)",
        "clock_format_bottom": "%H:%M:%S",
        "launcher_enabled": True,
        "launcher_items": [
            "capture",
            "audio",
            "ruler",
            "color_picker",
            "snippets",
            "bookmarks",
            "presenter",
        ],
        "launcher_item_size": 36,
        "launcher_close_delay_ms": 300,
    },
    # 空文字にすると、そのホットキーは登録されない(無効化できる)。
    "hotkeys": {
        "audio_toggle": "ctrl+alt+h",
        "capture_now": "ctrl+alt+r",
        "capture_sequence": "ctrl+alt+s",
        "mic_mute": "ctrl+alt+m",
        "color_picker": "ctrl+alt+c",
        "always_on_top": "ctrl+alt+t",
        "snippet_picker": "ctrl+alt+v",
        # あふｗ側でも同じ機能を J に割り当てているので、単独で呼ぶときも同じ指に置く。
        "launcher": "win+j",
    },
}

# settings.py 自身の置き場所を基準にする。cwd に依存させない。
SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """base(デフォルト)にoverride(読み込んだ値)を再帰的に重ねる。
    セクションごと上書きすると未指定キーが消えてしまうため、辞書同士は再帰的にマージする。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(path=None) -> dict:
    """settings.json を読み込む。存在しない/壊れている場合はデフォルトにフォールバックする。"""
    target_path = Path(path) if path else SETTINGS_PATH
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # ディープコピー

    if target_path.exists():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                merged = _deep_merge(merged, loaded)
        except (json.JSONDecodeError, OSError):
            # 壊れたJSONはそのまま使わず、デフォルト値で継続する
            pass
    else:
        save_settings(merged, target_path)

    return merged


def save_settings(settings_dict: dict, path=None) -> None:
    """settings.json へ書き込む。保存先フォルダが無ければ作成する。"""
    target_path = Path(path) if path else SETTINGS_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(settings_dict, f, ensure_ascii=False, indent=2)


def cleanup_old_captures(capture_settings: dict) -> None:
    """history_days > 0 のとき、保存フォルダ内のN日以上前のキャプチャファイルを削除する。
    デフォルト(0)では何もしない=自動削除しない。
    保存形式を変えても古いファイルが取り残されないよう rapture_*.* を対象にする。"""
    history_days = capture_settings.get("history_days", 0)
    if not history_days or history_days <= 0:
        return

    save_folder = Path(capture_settings.get("save_folder", DEFAULT_SETTINGS["capture"]["save_folder"]))
    if not save_folder.exists():
        return

    cutoff = time.time() - (history_days * 86400)

    for file_path in save_folder.glob("rapture_*.*"):
        try:
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink()
        except OSError:
            # 使用中などで削除できないファイルはスキップする
            pass
