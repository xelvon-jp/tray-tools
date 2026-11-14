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
#   goto_line {line}            指定行へ移動(line は1始まり。範囲外は
#                               文書の範囲に収めてから記録される)
#   next_edited_line {}         次の編集行へ移動(編集マークの行。
#                               無ければ記録されない)
#   prev_edited_line {}         前の編集行へ移動
#   toggle_bookmark {}          カーソル行のブックマークの付け外し
#   next_bookmark {}            次のブックマークへ移動(印が無ければ
#                               記録されない)
#   prev_bookmark {}            前のブックマークへ移動
#   move_line_up {count, extend}    上の行へ
#   move_line_down {count, extend}  下の行へ
#   move_char_left {count, extend}  左へ
#   move_char_right {count, extend} 右へ
#   move_word_left {count, extend}  単語の頭へ(Ctrl+左)
#   move_word_right {count, extend} 次の単語の頭へ(Ctrl+右)
#   move_document_start {extend}    文書の先頭へ(Ctrl+Home)
#   move_document_end {extend}      文書の末尾へ(Ctrl+End)
#   move_page_up {count, extend}    1画面分上へ(PageUp)
#   move_page_down {count, extend}  1画面分下へ(PageDown)
#   select_all {}                   すべて選択(Ctrl+A)
#   sort_lines {order}          行の並べ替え(order は "asc" / "desc")
#   remove_duplicate_lines {}   重複行の削除
#   reformat_text {}            整形(句点で改行、読点は桁数を超えるとき)
#   convert_halfwidth {}        選択範囲を全角 → 半角
#   convert_fullwidth {}        選択範囲を半角 → 全角
#   convert_lower {}            選択範囲の英字を大文字 → 小文字
#   convert_upper {}            選択範囲の英字を小文字 → 大文字
#   convert_tab_to_space {}     対象行範囲のタブをスペースに広げる
#   convert_space_to_tab {}     対象行範囲の行頭のスペースをタブに直す
#   convert_lead_half_to_full {}    行頭のスペースを全角優先に組み直す
#   convert_lead_full_to_half {}    行頭のスペースを半角に組み直す
#
# 記録から外しているもの(窓側の作法と合わせて):
#   ファイル操作(開く・保存・新規)、Copilot への送信、表示の切り替え、
#   矩形選択での編集(複数行にまたがる変化は切り分けられない)、
#   元に戻す・やり直しの最中の変化(再生時点の履歴の状態に依存する)、
#   マウスでのカーソル移動(座標では再現できない)。
#   選択範囲を打って上書きする入力は、選択範囲の削除 + 挿入として
#   記録する(選択がキーボードで作られた流れなら再生でも一致する。
#   選択範囲と変わった範囲が一致しない置き換えは記録しない)。
#
# テキスト形式(言語化。commands_to_text / commands_from_text):
#   1行1命令で、人が読んで直せる形にする。文法は単純に保つ:
#     # name: 名前        マクロの名前(コメント行のうち、この形だけ拾う)
#     # コメント          # で始まる行と空行は無視する
#     命令名              引数の無い命令は名前だけ
#     命令名 key=値 ...   引数は key=値 の並び。値は JSON のリテラル
#                         (文字列は "…" でエスケープ可、数値、true/false)
#     insert_text "文字列"  insert_text だけの簡便形(text="…" でもよい)
#   壊れた行は「N 行目」と行番号つきで ValueError にする(黙って
#   飛ばすと、書いたつもりの手順が欠けるため。JSON 版と同じ作法)。
#
# 登録マクロ(sanitize_registered): 設定の editor 節に
#   {名前: {"commands": [...], "key": "Ctrl+Alt+1"}} の形で持つ。
#   値は JSON の形のまま(命令の列の検証は _checked_commands を共用)。
#   値が命令の列そのもの([..])なら、キーなしの登録として受ける。
#   Python 形式の登録は {"python": "ソース", "key": "..."} で、
#   commands の代わりに python を持つ(整えた形でもそのまま。
#   構文が読めないものは落とす。検証は editor/macro_py.py)。
import json
import re

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
    "goto_line",
    "next_edited_line",
    "prev_edited_line",
    "toggle_bookmark",
    "next_bookmark",
    "prev_bookmark",
    "move_line_up",
    "move_line_down",
    "move_char_left",
    "move_char_right",
    "move_word_left",
    "move_word_right",
    "move_document_start",
    "move_document_end",
    "move_page_up",
    "move_page_down",
    "select_all",
    "sort_lines",
    "remove_duplicate_lines",
    "reformat_text",
    "convert_halfwidth",
    "convert_fullwidth",
    "convert_lower",
    "convert_upper",
    "convert_tab_to_space",
    "convert_space_to_tab",
    "convert_lead_half_to_full",
    "convert_lead_full_to_half",
})

# count を合算してまとめられる命令(同じ動きの繰り返し)。
_COALESCE_COUNT = frozenset({
    "delete_backward",
    "delete_forward",
    "move_line_up",
    "move_line_down",
    "move_char_left",
    "move_char_right",
    "move_word_left",
    "move_word_right",
    "move_page_up",
    "move_page_down",
})


def macro_to_json(commands, name="") -> str:
    """命令の列を JSON 文字列にする。

    引数の値が文字列・数値・真偽でないもの(None / list / dict)は、
    ここで ValueError にする(JSON では書けてしまうが、テキスト形式
    への書き出しや再生で意味が変わるため。_check_scalar_args)。"""
    for entry in commands:
        _check_scalar_args(entry.get("args") or {},
                           f"{entry.get('cmd')} の ")
    payload = {
        "name": name,
        "commands": [
            {"cmd": entry["cmd"], "args": dict(entry.get("args") or {})}
            for entry in commands
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# 引数の値に書けるもの(JSON のスカラー)。None / list / dict は
# 書けない(_check_scalar_args)。
_SCALAR_TYPES = (str, int, float, bool)


def _check_scalar_args(args, where):
    """引数の値を確かめる。文字列・数値・真偽だけが書ける。

    None / list / dict が入ると、JSON では読めるのに再生で意味が
    変わり(insert_text の text が None なら ""、list なら "[1, 2]"
    という文字列が入る)、テキスト形式では読み込み時に ValueError
    になる(値の種類が決まっているため)。往復できない値は、保存
    する側(macro_to_json / commands_to_text)と読み込む側
    (_checked_commands)の両方で落とす。"""
    for key, value in (args or {}).items():
        if not isinstance(value, _SCALAR_TYPES):
            raise ValueError(
                f"{where}{key} の値には、文字列・数値・真偽だけが書けます")


def validate_macro_name(name) -> str:
    """登録名を確かめて、前後空白を取った名前を返す。

    空の名前と、改行を含む名前は ValueError。改行が入るとテキスト
    形式の「# name: 名前」の行が壊れて読み戻せなくなるため。
    前後空白は取る(読み込み側も取るので、取らないと保存と読み込み
    で名前がずれる)。"""
    text = str(name or "").strip()
    if not text:
        raise ValueError("マクロの名前が空です")
    if "\n" in text or "\r" in text:
        raise ValueError("マクロの名前に改行は使えません")
    return text


def _checked_commands(raw):
    """命令の列(生の値)を検証して、{"cmd": ..., "args": {...}} の
    並びにして返す。形が壊れていたら ValueError。"""
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
        _check_scalar_args(args, f"{i + 1} 件目の ")
        commands.append({"cmd": entry["cmd"], "args": dict(args)})
    return commands


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
    return name, _checked_commands(raw)


# ---------------------------------------------------------------
# テキスト形式(言語化)
# ---------------------------------------------------------------
_NAME_LINE = "# name:"
_KEY_EQ_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_DECODER = json.JSONDecoder()
# str.splitlines() が行の区切りとみなす文字のうち、JSON の文字列
# としてそのまま書かれてしまうもの。json.dumps は U+0000〜U+001F
# を \uXXXX に逃がすが、この3つは文字として残る。テキスト形式は
# 1行1命令なので、書き出しでは \uXXXX に逃がし、読み込みでは
# "\n" だけで割る(読み込み側は commands_from_text)。
_LINE_BREAK_CHARS = ("\u2028", "\u2029", "\u0085")


def _json_literal(value) -> str:
    """値を JSON リテラルにする。行の区切りになる文字は、1行に
    収まるよう \\uXXXX の形へ逃がす。"""
    text = json.dumps(value, ensure_ascii=False)
    for char in _LINE_BREAK_CHARS:
        text = text.replace(char, "\\u%04x" % ord(char))
    return text


def commands_to_text(commands, name="") -> str:
    """命令の列をテキスト形式(1行1命令)にする。

    先頭に「# name: 名前」の行を置く(名前があるとき)。名前は
    validate_macro_name で確かめる(改行が入ると行が壊れて読み戻せ
    ないため)。引数の値は JSON リテラルで書くので、文字列の中の
    改行や引用符はエスケープされて1行に収まる。U+2028 / U+2029 /
    U+0085 も \\uXXXX の形に逃がす(そのまま書くと、読み込み時の行の
    切り分けがずれる)。引数の値が文字列・数値・真偽でないものは、
    ここで ValueError にする(書いたテキストが読めなくなるため)。
    insert_text は text が文字列のときにだけ簡便形
    (insert_text "…")で書く(数値などは key=値の形でないと、
    読み込み側が文字列でないと受け付けないため)。"""
    lines = []
    if name:
        name = validate_macro_name(name)
        lines.append(f"{_NAME_LINE} {name}")
    for entry in commands:
        cmd = entry["cmd"]
        args = dict(entry.get("args") or {})
        _check_scalar_args(args, f"{cmd} の ")
        if not args:
            lines.append(cmd)
        elif (cmd == "insert_text" and set(args) == {"text"}
                and isinstance(args["text"], str)):
            lines.append("insert_text " + _json_literal(args["text"]))
        else:
            parts = [cmd]
            for key, value in args.items():
                parts.append(f"{key}={_json_literal(value)}")
            lines.append(" ".join(parts))
    return ("\n".join(lines) + "\n") if lines else ""


def _decode_value(text, pos, lineno):
    """text の pos から JSON リテラルを1つ読む。(値, 終わり位置)。"""
    try:
        value, end = _DECODER.raw_decode(text, pos)
    except json.JSONDecodeError as e:
        raise ValueError(f"{lineno} 行目の値が読めません（{e.msg}）") from e
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(
            f"{lineno} 行目の値には、文字列・数値・真偽だけが書けます")
    return value, end


def _parse_args(cmd, rest, lineno):
    """命令名の後ろ(rest)を引数の辞書にする。"""
    if cmd == "insert_text" and not _KEY_EQ_RE.match(rest):
        # 簡便形: JSON 文字列そのものが text になる。
        value, end = _decode_value(rest, 0, lineno)
        if not isinstance(value, str) or rest[end:].strip():
            raise ValueError(
                f"{lineno} 行目: insert_text の後ろは文字列1つだけに"
                "してください")
        return {"text": value}
    args = {}
    pos = 0
    while pos < len(rest):
        match = _KEY_EQ_RE.match(rest, pos)
        if match is None:
            raise ValueError(
                f"{lineno} 行目の引数が key=値 の形になっていません")
        value, end = _decode_value(rest, match.end(), lineno)
        args[match.group(1)] = value
        pos = end
        while pos < len(rest) and rest[pos] in " \t":
            pos += 1
    return args


def commands_from_text(text: str):
    """テキスト形式から (名前, 命令の列) を返す。

    壊れた行があったら、その行番号つきで ValueError(黙って飛ばす
    と、書いたつもりの手順が欠けるため)。行の切り分けは "\n" だけ
    で行う(splitlines は U+2028 / U+2029 / U+0085 も行の区切りと
    みなすため、insert_text の文字列にそれらが入ると行が切れて
    再生できなくなる)。"""
    name = ""
    commands = []
    for lineno, raw_line in enumerate(str(text).split("\n"), 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not name and line.startswith(_NAME_LINE):
                name = line[len(_NAME_LINE):].strip()
            continue
        parts = line.split(None, 1)
        cmd = parts[0]
        if cmd not in KNOWN_COMMANDS:
            raise ValueError(
                f"{lineno} 行目の命令が不正です（{cmd!r}）")
        rest = parts[1].strip() if len(parts) > 1 else ""
        args = _parse_args(cmd, rest, lineno) if rest else {}
        commands.append({"cmd": cmd, "args": args})
    return name, commands


# ---------------------------------------------------------------
# 登録マクロ(設定に持つ形の整え)
# ---------------------------------------------------------------
def copy_commands(commands):
    """命令の列の写し(args も含めて新しい辞書にする)。"""
    return [{"cmd": entry["cmd"], "args": dict(entry.get("args") or {})}
            for entry in commands]


def sanitize_registered(raw):
    """登録マクロの設定値を整える。

    形は {名前: {"commands": [...], "key": "..."}}。値が命令の列
    そのもの([..])なら、キーなしの登録として受ける。壊れた項目
    (知らない命令を含む・命令が空・引数の値が文字列・数値・真偽
    でない・名前が空か改行を含むなど)は黙って落とす(設定ファイル
    の書き間違いで窓が開かなくなるのを防ぐため。他の設定の整えと
    同じ作法)。名前は前後空白を取って揃え、取った結果が重なったら
    先のものを残して後のものを落とす(黙って上書きすると、保存した
    はずのマクロが消えるため)。キーの重なり(同じキーを2つのマクロ
    が持つ)はここでは落とさない(窓側が先のものを優先し、管理画面
    が「効いていない」ことを表示する。どちらかを捨てるより、
    直せる形で残すほうがましなため)。"""
    clean = {}
    if not isinstance(raw, dict):
        return clean
    for raw_name, entry in raw.items():
        try:
            name = validate_macro_name(raw_name)
        except ValueError:
            continue
        if name in clean:
            continue
        if isinstance(entry, list):
            commands_raw, key, source = entry, "", None
        elif isinstance(entry, dict):
            commands_raw = entry.get("commands")
            key = entry.get("key")
            key = key.strip() if isinstance(key, str) else ""
            source = entry.get("python")
        else:
            continue
        if isinstance(source, str) and source.strip():
            # Python 形式の登録。構文が読めないものは落とす
            # (検証は macro_py。import はここだけ遅らせる)。
            try:
                from editor import macro_py
                invalid = macro_py.validate_python(source)
            except Exception:
                invalid = "検証できませんでした"
            if invalid is not None:
                continue
            clean[name] = {"python": source, "key": key}
            continue
        try:
            commands = _checked_commands(commands_raw)
        except ValueError:
            continue
        if not commands:
            continue
        clean[name] = {"commands": commands, "key": key}
    return clean


def copy_registered(mapping):
    """登録マクロの対応(整えた形)の写し。"""
    copied = {}
    for name, entry in (mapping or {}).items():
        if "python" in entry:
            copied[name] = {"python": entry["python"],
                            "key": entry.get("key") or ""}
        else:
            copied[name] = {
                "commands": copy_commands(entry["commands"]),
                "key": entry.get("key") or ""}
    return copied


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
        if added > 0 and removed > 0:
            # 選択範囲を打鍵で上書きした置き換えは、「選択範囲の
            # 削除 + 挿入」に切り分けられる(選択がキーボードで
            # 作られた流れなら、再生でも同じ結果になる)。
            if (sel_start is not None and sel_end is not None
                    and sel_start == pos and sel_end == pos + removed):
                self.note_command("delete_selection", {})
                self.note_command("insert_text", {"text": inserted_text})
                if self.recording:
                    self._insert_end = pos + added
                return True
            return False
        # それ以外の複合の変化は記録しない。
        return False
