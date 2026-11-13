# editor/keymap.py
# エディタのキー割り当て。命令の名前 → QKeySequence の文字列、の小さな表。
#
# 秀丸寄りの操作感を「あとで変えられる」形にするための置き場所。窓は
# この表を見て QAction にショートカットを付けるので、割り当てを変える
# ときはここ(settings.json の editor.keymap で上書きも可)だけを触る。
# 元に戻す・やり直し・切り取り等の編集キーは QPlainTextEdit の標準
# (Ctrl+Z / Ctrl+Y / Ctrl+X 等)をそのまま使うので、この表には入れない。
#
# 変更はキー割り当ての画面(editor/keymap_dialog.py)からも行える。
# 画面は COMMAND_LABELS の並びで一覧し、変えた分だけを上書き分
# (diff_overrides)として設定へ流す。割り当てを外した命令は空文字列で
# 表し、merge_keymap はそれを「割り当てなし」の明示として受け取る
# (外したまま再起動しても、既定が勝手に戻らないようにするため)。

DEFAULT_KEYMAP = {
    "new": "Ctrl+N",
    "open": "Ctrl+O",
    "save": "Ctrl+S",
    "save_as": "F12",
    "close_window": "Ctrl+W",
    "find": "Ctrl+F",
    "find_next": "F3",
    "find_prev": "Shift+F3",
    "find_all": "Ctrl+Shift+G",
    "goto_line": "Ctrl+G",
    "next_edited_line": "F2",
    "prev_edited_line": "Shift+F2",
    "toggle_bookmark": "F7",
    "next_bookmark": "F8",
    "prev_bookmark": "Shift+F8",
    "bookmark_list": "Ctrl+Shift+B",
    "send_selection": "Ctrl+Shift+C",
    "cut_to_end": "Ctrl+K",
    "delete_line": "Ctrl+Shift+K",
    "repeat_last": "F4",
    "reformat_text": "Ctrl+Shift+F",
}


# 命令の表示名(キー割り当ての画面に出す並びと名前)。並びは
# DEFAULT_KEYMAP と同じで、名前はメニューの文言にそろえてある。
COMMAND_LABELS = [
    ("new", "新規"),
    ("open", "開く"),
    ("save", "上書き保存"),
    ("save_as", "名前を付けて保存"),
    ("close_window", "閉じる"),
    ("find", "検索"),
    ("find_next", "次を検索"),
    ("find_prev", "前を検索"),
    ("find_all", "すべて検索"),
    ("goto_line", "指定行へ移動"),
    ("next_edited_line", "次の編集行へ"),
    ("prev_edited_line", "前の編集行へ"),
    ("toggle_bookmark", "ブックマークの付け外し"),
    ("next_bookmark", "次のブックマークへ"),
    ("prev_bookmark", "前のブックマークへ"),
    ("bookmark_list", "ブックマーク一覧"),
    ("send_selection", "選択範囲を Copilot に投げる"),
    ("cut_to_end", "行末まで切り取り"),
    ("delete_line", "行を削除"),
    ("repeat_last", "直前の操作を繰り返す"),
    ("reformat_text", "整形"),
]


def command_label(name):
    """命令の表示名。知らない命令名はそのまま返す。"""
    for key, label in COMMAND_LABELS:
        if key == name:
            return label
    return name


def sanitize_overrides(overrides=None):
    """設定から読んだ上書き分を整える。

    知らない命令名と、文字列でない値は捨てる(設定ファイルの
    書き間違いで窓が開かなくなるのを防ぐ)。空文字列は「割り当て
    なし」の明示として残す。"""
    clean = {}
    if isinstance(overrides, dict):
        for name, keys in overrides.items():
            if name in DEFAULT_KEYMAP and isinstance(keys, str):
                clean[name] = keys
    return clean


def merge_keymap(overrides=None):
    """DEFAULT_KEYMAP に上書き分を重ねた表を返す。

    知らない命令名と、文字列でない値は捨てる(sanitize_overrides)。
    空文字列は「割り当てなし」として受け取り、その命令は既定の
    キーも持たない。"""
    table = dict(DEFAULT_KEYMAP)
    for name, keys in sanitize_overrides(overrides).items():
        table[name] = keys
    return table


def diff_overrides(table):
    """実効の表から、既定と違う項目だけの上書き分を返す。

    割り当てを外した命令は空文字列で残す(既定に戻すのではなく、
    「なし」が利用者の決めた状態であるため)。"""
    result = {}
    if isinstance(table, dict):
        for name, default in DEFAULT_KEYMAP.items():
            keys = table.get(name, default)
            if isinstance(keys, str) and keys != default:
                result[name] = keys
    return result
