# editor/macros.py
# エディタのマクロ。記録はキーストロークではなく「命令の列」として
# 持つ(例: {"cmd": "insert_text", "args": {"text": "abc"}})。JSON で
# 保存でき、手で書いたマクロも同じ形で読める。
#
# このファイルは Qt に依存しない。命令の実行は窓(editor/window.py)
# の命令層が持ち、ここは「記録の材料の切り分け」と「JSON の読み
# 書き」だけを持つ。
#
# 記録できる命令(KNOWN_COMMANDS):
#   insert_text {text}          文字の挿入(続けて打った分は1つにまとめる)
#   delete_backward {count}     後方へ count 文字の削除(BS)
#   delete_forward {count}      前方へ count 文字の削除(Del)
#   delete_selection {}         選択範囲の削除
#   cut_to_end {}               行末まで切り取り
#   delete_line {}              行の削除
#   find_next {text, case, regex}   次を検索(使った語と区別と
#                                   正規表現かどうかを控える)
#   find_prev {text, case, regex}   前を検索
#   replace_next {text, repl, case, regex}  次の1件を置換
#   replace_all {text, repl, case, regex}   全部置換
#   move_line_start {extend}    行頭へ
#   move_line_end {extend}      行末へ
#   move_line_up {count, extend}    上の行へ
#   move_line_down {count, extend}  下の行へ
#   move_char_left {count, extend}  左へ
#   move_char_right {count, extend} 右へ
#   sort_lines {order}          行の並べ替え(order は "asc" / "desc")
#   remove_duplicate_lines {}   重複行の削除
#   convert_halfwidth {}        選択範囲を全角 → 半角
#   convert_fullwidth {}        選択範囲を半角 → 全角
#   convert_lower {}            選択範囲の英字を大文字 → 小文字
#   convert_upper {}            選択範囲の英字を小文字 → 大文字
#
# 記録から外しているもの(窓側の作法と合わせて):
#   ファイル操作(開く・保存・新規)、Copilot への送信、表示の切り替え、
#   矩形選択での編集(複数行にまたがる変化は切り分けられない)、
#   選択範囲を打って上書きする入力(置き換えの変化)、
#   元に戻す・やり直しの最中の変化、マウスでのカーソル移動。
import json

KNOWN_COMMANDS = frozenset({
    "insert_text",
    "delete_backward",
    "delete_forward",
    "delete_selection",
    "cut_to_end",
    "delete_line",
    "find_next",
    "find_prev",
    "replace_next",
    "replace_all",
    "move_line_start",
    "move_line_end",
    "move_line_up",
    "move_line_down",
    "move_char_left",
    "move_char_right",
    "sort_lines",
    "remove_duplicate_lines",
    "convert_halfwidth",
    "convert_fullwidth",
    "convert_lower",
    "convert_upper",
})

# count を合算してまとめられる命令(同じ動きの繰り返し)。
_COALESCE_COUNT = frozenset({
    "delete_backward",
    "delete_forward",
    "move_line_up",
    "move_line_down",
    "move_char_left",
    "move_char_right",
})


def macro_to_json(commands, name="") -> str:
    """命令の列を JSON 文字列にする。"""
    payload = {
        "name": name,
        "commands": [
            {"cmd": entry["cmd"], "args": dict(entry.get("args") or {})}
            for entry in commands
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def macro_from_json(text: str):
    """JSON 文字列から (名前, 命令の列) を返す。

    形は {"name": ..., "commands": [...]} が本筋だが、命令の列だけの
    JSON([...]) も受け付ける(手で書くときの簡便さのため)。命令名が
    知らないものだったり args が辞書でなかったりしたら ValueError
    (黙って飛ばすと、書いたつもりの手順が欠けるため)。"""
    data = json.loads(text)
    if isinstance(data, list):
        name, raw = "", data
    elif isinstance(data, dict):
        name = str(data.get("name") or "")
        raw = data.get("commands")
    else:
        raise ValueError("マクロの JSON は、命令の列か {\"commands\": [...]} の形にしてください")
    if not isinstance(raw, list):
        raise ValueError("commands が命令の列になっていません")
    commands = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or entry.get("cmd") not in KNOWN_COMMANDS:
            bad = entry.get("cmd") if isinstance(entry, dict) else entry
            raise ValueError(f"{i + 1} 件目の命令が不正です（{bad!r}）")
        args = entry.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError(f"{i + 1} 件目の args が辞書になっていません")
        commands.append({"cmd": entry["cmd"], "args": dict(args)})
    return name, commands


class MacroRecorder:
    """記録の材料を命令の列に切り分ける。

    窓が「起きたこと」を2つの口から渡す:
      note_command(命令名, 引数) … 命令層を通った操作(切り取り・
        検索・キーでの移動)。F4 のために、記録中でなくても
        last_command だけは常に更新する。
      feed_change(...) … 文書の変化(contentsChange)。打鍵や
        Backspace/Delete、貼り付けなどの「命令層を通らない編集」
        を、挿入と削除に切り分ける。

    last_command は (命令名, 引数) の組か None。F4 はこれをもう
    一度実行する。まとめ(count の合算や挿入の連結)は commands の
    側だけの話で、last_command は常に「最後の1回の分」を持つ。"""

    def __init__(self):
        self.recording = False
        self.commands = []
        self.last_command = None
        self._insert_end = None    # 直近の挿入が終わった文書位置

    def start(self):
        self.recording = True
        self.commands = []
        self._insert_end = None

    def stop(self):
        self.recording = False
        self._insert_end = None

    def break_insert_chain(self):
        """挿入の連結を切る(文書の差し替え等、記録の対象外の変化の
        あとで呼ぶ)。"""
        self._insert_end = None

    def clear_last_command(self):
        """F4 の控えを消す。記録できない操作(矩形編集・選択の上書き・
        マウスでの移動など)が起きたら呼ぶ。その操作自体が「直前の
        操作」なので、それより前の命令を F4 で繰り返すと、見た目と
        違う結果になる。"""
        self.last_command = None

    # ---------------------------------------------------------------
    def note_command(self, name, args=None):
        """命令層を通った操作を控える。"""
        args = dict(args or {})
        self.last_command = (name, dict(args))
        if name != "insert_text":
            self._insert_end = None
        if not self.recording:
            return
        if name in _COALESCE_COUNT and self.commands:
            prev = self.commands[-1]
            if prev["cmd"] == name and all(
                    prev["args"].get(k) == v
                    for k, v in args.items() if k != "count"):
                prev["args"]["count"] = (int(prev["args"].get("count", 1))
                                         + int(args.get("count", 1)))
                return
        self.commands.append({"cmd": name, "args": args})

    def feed_change(self, pos, removed, added, inserted_text,
                    sel_start, sel_end, cursor_before):
        """文書の変化1回分を切り分ける。

        pos / removed / added は contentsChange の値。inserted_text
        は追加された文字列(窓が文書から切り出す)。sel_start /
        sel_end は変化の直前の選択範囲、cursor_before は変化の
        直前のカーソル位置(削除の向きの判定に使う)。

        記録できる操作として切り分けたら True、記録できない変化
        (置き換え、分類できない削除など)なら False を返す。窓は
        False のときに「記録できない操作」として扱う(件数の知らせ
        と、F4 の控えの消去)。記録中でなくても last_command の更新
        と、この成否は同じに返す(F4 は記録と無関係に効くため)。"""
        if added > 0 and removed == 0:
            # 純粋な挿入。直前の挿入と地続きなら1つの命令にまとめる。
            self.last_command = ("insert_text", {"text": inserted_text})
            if self.recording:
                if (self._insert_end == pos and self.commands
                        and self.commands[-1]["cmd"] == "insert_text"):
                    self.commands[-1]["args"]["text"] += inserted_text
                else:
                    self.commands.append(
                        {"cmd": "insert_text", "args": {"text": inserted_text}})
                self._insert_end = pos + added
            return True
        self._insert_end = None
        if removed > 0 and added == 0:
            # 純粋な削除。選択範囲の削除か、BS か、Del か。
            if (sel_start is not None and sel_end is not None
                    and sel_end > sel_start
                    and sel_start == pos and sel_end == pos + removed):
                self.note_command("delete_selection", {})
                return True
            if cursor_before == pos + removed:
                self.note_command("delete_backward", {"count": removed})
                return True
            if cursor_before == pos:
                self.note_command("delete_forward", {"count": removed})
                return True
            # どれでもない削除(他所の範囲が消えた等)は記録しない。
            return False
        # 置き換え(削除+挿入)や複合の変化は記録しない。
        return False
