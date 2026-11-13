# editor/keymap.py
# エディタのキー割り当て。命令の名前 → QKeySequence の文字列、の小さな表。
#
# 秀丸寄りの操作感を「あとで変えられる」形にするための置き場所。窓は
# この表を見て QAction にショートカットを付けるので、割り当てを変える
# ときはここ(settings.json の editor.keymap で上書きも可)だけを触る。
# 元に戻す・やり直し・切り取り等の編集キーは QPlainTextEdit の標準
# (Ctrl+Z / Ctrl+Y / Ctrl+X 等)をそのまま使うので、この表には入れない。

DEFAULT_KEYMAP = {
    "new": "Ctrl+N",
    "open": "Ctrl+O",
    "save": "Ctrl+S",
    "save_as": "F12",
    "close_window": "Ctrl+W",
    "find": "Ctrl+F",
    "find_next": "F3",
    "find_prev": "Shift+F3",
    "goto_line": "Ctrl+G",
    "send_selection": "Ctrl+Shift+C",
    "cut_to_end": "Ctrl+K",
    "delete_line": "Ctrl+Shift+K",
    "repeat_last": "F4",
    "reformat_text": "Ctrl+Shift+F",
}


def merge_keymap(overrides=None):
    """DEFAULT_KEYMAP に上書き分を重ねた表を返す。

    知らない命令名と、文字列でない値は捨てる(設定ファイルの書き間違いで
    窓が開かなくなるのを防ぐ)。"""
    table = dict(DEFAULT_KEYMAP)
    if isinstance(overrides, dict):
        for name, keys in overrides.items():
            if name in table and isinstance(keys, str) and keys:
                table[name] = keys
    return table
