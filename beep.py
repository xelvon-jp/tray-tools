# beep.py
# 音を1つ鳴らすだけの部品。トレイアイコンは持たない(keep_awake.py と同じ立ち位置)。
#
# トーストより強い合図が要る場面がある。画面を見ていない・別のウィンドウを見ている
# ときでも、音なら気づける。長い処理の終わりや失敗を外から知らせる用。
#
# winsound.MessageBeep はシステムの「イベントの音」を鳴らして即座に戻る(非同期)。
# winsound.Beep(周波数, 長さ) は鳴り終わるまで戻らないので使わない——Qt のメインスレッドを
# その間だけ止めてしまう。音の種類はOSのサウンド設定に従うので、ユーザーが消していれば
# 鳴らない(それでよい。こちらで音量やデバイスを勝手に触らない)。
import sys
import winsound

# 外から名前で選べる音。値は user32 の MessageBeep に渡す uType。
# 名前は「何を知らせたいか」で付ける(どのシステム音かではなく)。
KINDS = {
    "ok": winsound.MB_OK,                    # 区切り。いちばん軽い
    "done": winsound.MB_ICONASTERISK,        # 完了(情報音)
    "warn": winsound.MB_ICONEXCLAMATION,     # 警告
    "error": winsound.MB_ICONHAND,           # 失敗
    "ask": winsound.MB_ICONQUESTION,         # 確認待ち
}
DEFAULT_KIND = "done"


def kind_names() -> str:
    """選べる名前を並べた文字列。引数を間違えたときに返す案内用。"""
    return " / ".join(KINDS)


def play(kind: str = DEFAULT_KIND) -> bool:
    """音を1つ鳴らす。知らない名前なら鳴らさずに False。

    外から叩かれる口から呼ばれるので、想定外の引数は「何もしない」に倒す
    (勝手に別の音を鳴らすと、何を知らせたのか分からなくなる)。
    例外は投げない——PySide6 はスロットから例外が抜けると常駐アプリごと終了する。"""
    uType = KINDS.get((kind or DEFAULT_KIND).strip().lower())
    if uType is None:
        return False
    try:
        winsound.MessageBeep(uType)
        return True
    except RuntimeError as e:
        print(f"[tray-tools] 音を鳴らせません: {e}", file=sys.stderr)
        return False
