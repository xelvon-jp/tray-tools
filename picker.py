# picker.py
# 枠なしの小さな選択ウインドウ。定型文(snippets.py)とフォルダブックマーク(launcher.py)で
# 同じ窓を使うため、元は SnippetPicker だったものを汎用化して切り出した部品。
#
# 「表示名で絞り込んで1つ決める」以上のことはしない。決めた後に何をするか(コピー・移動・
# 登録)は呼び出し側の on_accept が行い、ウインドウの開閉はこちらが受け持つ。
import ctypes
import fnmatch
import sys

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

# 幅の下限。実際の幅は _fit_to_items が中身に合わせて広げる(上限は画面幅の
# MAX_WIDTH_RATIO)。短い一覧ではこの幅のまま。
WINDOW_WIDTH = 420

# プレビューを出すときは横も広げる。展開後の本文を折り返しだらけで見せても
# 「何がコピーされるか」が読み取れないため。
PREVIEW_WINDOW_WIDTH = 560

# 一覧に見せる行数。項目数に応じてこの範囲で高さを伸縮させる。少なすぎると
# 探しにくく、多すぎると画面を覆ってしまうので上下に枠を設ける。
MIN_VISIBLE_ROWS = 8
MAX_VISIBLE_ROWS = 20

# プレビューを出すときは一覧を控えめにする。両方を最大まで伸ばすと画面を覆ってしまう。
PREVIEW_MAX_VISIBLE_ROWS = 12

# プレビュー領域とキーヒント1行の高さ(px)。行数×フォント高さで求めない。
# QT_QPA_PLATFORM=offscreen ではフォントメトリクスが当てにならず、測った値だけを
# 根拠にすると環境によって潰れたり伸びたりするため、ここは固定値で持つ。
#
# 150px だと 8行ほどしか映らず、13行のテンプレートで下が切れていた(実測で
# 縦に6行ぶん、横に 325px はみ出していた)。280px で手元の最長テンプレートが
# 収まったので、少し余裕を見て 300px にしてある。選んだものが
# 何なのかを確かめる欄なので、まず「収まること」を優先して広げる。中身に合わせて
# 伸縮させないのは、↑↓ を動かすたびに窓の高さと位置が変わって目が落ち着かないため
# (項目数で高さを決めないのと同じ理由)。呼び出し側から preview_height で変えられる。
PREVIEW_HEIGHT = 300
HINT_LINE_HEIGHT = 15

# プレビューの高さとして受け付ける範囲(px)。設定ファイルに極端な値が入っても、
# 窓が潰れたり画面を覆ったりしないように挟む。
PREVIEW_HEIGHT_MIN = 80
PREVIEW_HEIGHT_MAX = 600

# 画面の作業領域に対して、ウインドウが占めてよい高さの上限。
MAX_SCREEN_RATIO = 0.8

# 幅の上限(画面の作業領域に対する割合)。中身に合わせて広げるときの頭打ち。
MAX_WIDTH_RATIO = 0.9

# 中央より少し上に出す。ちょうど中央だと視線移動が大きく、下端が画面の下寄りになる。
VERTICAL_OFFSET = -200

# 一覧の行の余白(px)。QListWidget の既定は行間が広く、一度に見える数が減る。
ITEM_PADDING_V = 1
ITEM_PADDING_H = 6

DEFAULT_PLACEHOLDER = "絞り込み（↑↓で選択 / Enterで決定 / Escで閉じる）"

# 一覧の項目は Qt.UserRole に添字を持つ。既存項目(self._items)は 0 以上の添字、
# dynamic_items から作った項目は元リストに実体が無いので負数を持たせて区別する。
# i 番目の動的項目は -(i + 1)(0 は正負で区別できないので 1 からずらす)。


def dynamic_role(index: int) -> int:
    """動的項目の i 番目に持たせる UserRole の値。"""
    return -(index + 1)


def split_item(entry):
    """一覧の項目を (表示名, データ, 絞り込み名) にそろえる。

    項目は (表示名, データ) が基本。3つ目に絞り込み名を足した項目では、
    絞り込みを表示名ではなくそちらで判定する(表示名の頭に種別の印を付けても、
    本来の名前で絞り込めるようにするため。統合ピッカー用)。"""
    if len(entry) >= 3:
        return entry[0], entry[1], entry[2]
    return entry[0], entry[1], entry[0]


def wildcard_match(name: str, keyword: str) -> bool:
    """`*` / `?` を含む keyword の照合(名前で検索の `*` と同じ考え方)。

    keyword を空白で割り、各語をグロブとして名前の中に探す(語は順不同)。
    前方一致ではなく「名前の中にパターンが現れるか」で見るので、
    `Exe*` でも `*cel` でも Excel に当たる。"""
    lowered = name.lower()
    return all(fnmatch.fnmatchcase(lowered, f"*{token}*")
               for token in keyword.split())


def name_matches(name: str, keyword: str) -> bool:
    """絞り込みの判定そのもの。keyword は strip + lower 済みの前提。

    既定は前方一致。`*` / `?` が含まれるときだけワイルドカード照合に
    切り替える(wildcard_match)。_refresh_list の絞り込みと、候補を数える
    filter_names で同じ関数を使う(判定を二重に持たない。work-board#15
    の決定事項)。"""
    if not keyword:
        return True
    if "*" in keyword or "?" in keyword:
        return wildcard_match(name, keyword)
    return name.lower().startswith(keyword)


def filter_names(names, text: str) -> list:
    """text で絞り込んだときに残る表示名。_refresh_list と同じ判定。"""
    keyword = text.strip().lower()
    return [name for name in names if name_matches(name, keyword)]


def is_deletion(old: str, new: str) -> bool:
    """new が old から連続した範囲を消しただけ(純粋な削除)なら True。

    BackSpace / Delete は「いつでも効く」ものなので、候補の数によらず
    通すための判定。選択範囲を消す操作も、消す範囲が連続していれば
    ここに含まれる。"""
    if len(new) >= len(old):
        return False
    removed = len(old) - len(new)
    return any(new == old[:i] + old[i + removed:]
               for i in range(len(new) + 1))


def accepts_input(old_text: str, new_text: str,
                  old_count: int, new_count: int) -> bool:
    """入力欄の変更を受け付けるか。候補が0件になる編集だけを止める。

    old_count / new_count は、それぞれの入力のときに実際に一覧へ出る行数
    (動的項目を含む)。判定の考え方:
    - 変更なし・純粋な削除はいつでも応じる
    - 今の入力ですでに候補が0件なら止めない(何も打っていない状態で
      候補が0件のときに、打つこと自体を封じて詰まらせないため)
    - それ以外は、新しい入力で候補が1件以上残るときだけ応じる"""
    if new_text == old_text:
        return True
    if is_deletion(old_text, new_text):
        return True
    if old_count <= 0:
        return True
    return new_count > 0


# 候補が0件になる入力を無効にしたときの合図。入力欄を一瞬だけこの色にする
# (音は出さない。work-board#15 の決定事項)。色を戻すまでの時間もここで持つ。
REJECT_FLASH_MS = 180
REJECT_FLASH_STYLE = "QLineEdit { background-color: #6e2434; }"


class PickerWindow(QWidget):
    """枠なしの小さな選択ウインドウ。上の入力欄で絞り込み、下のリストから1つ決める。

    items は [(表示名, 任意のデータ), ...]。3つ目に絞り込み名を足すと、絞り込みは
    表示名ではなくそちらで判定する(split_item)。決定すると on_accept(表示名, データ) を呼び、
    その後このウインドウは必ず閉じて closed を出す(呼び出し側は参照を捨てるだけでよい)。
    on_accept は「閉じるかどうか」を決められない。二択にすると呼び出し側ごとに閉じ忘れの
    経路が生まれ、枠なし・最前面のウインドウが画面に残る事故になるため。

    preview_provider / hint / on_new / on_edit / on_open_folder はすべて任意。渡した
    ものだけが姿を現す。定型文には要るがフォルダブックマークには要らない、という差を
    ここで吸収する(共用の窓なので、既定では今までどおり「絞り込んで選ぶだけ」)。

    - preview_provider(データ) -> str … 選択中の項目の中身を下のプレビューに出す
    - preview_height … プレビュー欄の高さ(px)。既定は PREVIEW_HEIGHT
    - dynamic_items(入力欄のテキスト) -> [(表示名, データ), ...] … items に無い項目を
      入力から作って一覧の先頭に足す。渡すのは生のテキストで、strip も lower もしない
      (引用符や空白をどう扱うかは呼び出し側にしか決められない)。ここでも「テキストが
      何を意味するか」は解釈しない。返ってきた順にそのまま並べる(並べ替えない。
      どの順に見せたいかを知っているのは呼び出し側だけ)
    - tab_text(表示名, データ) -> str or None … Tab を押したときに入力欄へ入れる文字列。
      返すと入力欄がそれに差し替わり(カーソルは末尾)、textChanged で一覧が組み直る。
      窓は閉じない。渡した場合、None が返るときも Tab は食べる(Qtの既定のフォーカス
      移動で入力欄から抜けると、打ち続けられなくなって使いにくい)。渡さなければ
      Tab は今までどおり素通し
    - hint … ウインドウ下部に出す小さな説明(改行可)
    - on_new() / on_edit(表示名, データ) / on_open_folder() … Ctrl+N / Ctrl+E / Ctrl+O。
      いずれも実行後はこのウインドウを閉じる(外部エディタで直した結果を出すには
      開き直す必要があり、古い一覧を残しておくと食い違うため)。
    - on_delete(表示名, データ) / on_move(表示名, データ, step) … Ctrl+D / Ctrl+↑↓。
      こちらは閉じない。続けて2件消す・何段も動かす、をそのままやれるようにするため。
      呼び出し側は変更を反映した一覧を set_items で渡し直す(選択行の面倒はこちらで見る)。
      動的項目では呼ばない(設定ファイルに実体が無い)。
    - modes=[(予約文字, モード名, 色), ...] … モード切り替え(統合ピッカー用)。
      渡すと、入力欄の左端に予約文字の色付きチップ部品が出て、モード名が入力欄の
      背景(プレースホルダ)に薄く出る。開いた直後は initial_mode のモード。入力が
      空のときに予約文字を打つとそのモードへ移り、入力が空のときに Backspace を
      押すとチップが外れてモード無しになる。モード無しの間は一覧がモード切り替えの
      ヒント行になり、行を選ぶとそのモードへ移る。モードが変わるたびに
      on_mode_change(予約文字 or None) が呼ばれ、候補の差し替えは呼び出し側が
      set_items で行う(dynamic_items の中身も呼び出し側がモードで切り替える)。
      `=` は特殊で、選んだ瞬間にこの窓を閉じて on_calc(式) を呼ぶ(電卓は別窓)。
    - on_ctrl_enter(表示名, データ) … Ctrl+Enter。渡したときだけ効き、窓は閉じない
      (上にメニューを重ねるような使い方は呼び出し側が行う)。動的項目でも呼ばれる。"""

    closed = Signal()

    def __init__(
        self,
        title: str,
        items: list,
        on_accept,
        placeholder: str = None,
        preview_provider=None,
        hint: str = None,
        on_new=None,
        on_edit=None,
        on_open_folder=None,
        preview_height: int = None,
        dynamic_items=None,
        tab_text=None,
        on_delete=None,
        on_move=None,
        dynamic_last: bool = False,
        modes=None,
        initial_mode: str = None,
        on_mode_change=None,
        on_calc=None,
        on_ctrl_enter=None,
    ):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setWindowTitle(title)

        self._items = list(items)
        self._on_accept = on_accept
        self._preview_provider = preview_provider
        self._preview_height = max(
            PREVIEW_HEIGHT_MIN,
            min(int(preview_height or PREVIEW_HEIGHT), PREVIEW_HEIGHT_MAX),
        )
        self._on_new = on_new
        self._on_edit = on_edit
        self._on_open_folder = on_open_folder
        self._dynamic_items = dynamic_items
        self._tab_text = tab_text
        self._dynamic_last = dynamic_last
        self._on_delete = on_delete
        self._on_move = on_move
        # モード切り替え(modes を渡したときだけ)。チップは入力欄に重ねる部品で、
        # 予約文字そのものは入力欄の文字列には入らない。
        self._mode_list = list(modes) if modes else None
        self._mode_map = ({char: (name, color) for char, name, color in modes}
                          if modes else {})
        # 初期モードは候補の組み立てより前に決めておく(最初の _refresh_list で
        # ヒント行ではなく初期モードの一覧になるように)。`=` は開いた瞬間に
        # 電卓へ行く文字なので、初期モードにはしない。
        self._mode = (initial_mode if initial_mode in self._mode_map
                      and initial_mode != "=" else None)
        self._on_mode_change = on_mode_change
        self._on_calc = on_calc
        self._on_ctrl_enter = on_ctrl_enter
        self._mode_switching = False
        self._placeholder_base = placeholder or DEFAULT_PLACEHOLDER
        # 入力から作った項目。self._items には足さない(添字がずれると Qt側に持たせた
        # UserRole の意味が変わってしまう)。一覧に出ている間だけここに居る。
        self._dynamic = []
        self._closed_emitted = False
        # 決定は一度きり。Enterの押しっぱなしなど、閉じるまでの間に二度呼ばれても弾く。
        # Ctrl+N などの副作用を持つキーも、走り出したらこのフラグで後続を止める。
        self._committing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        font = QFont("Meiryo", 10)
        self.search = QLineEdit()
        self.search.setFont(font)
        self.search.setPlaceholderText(placeholder or DEFAULT_PLACEHOLDER)
        self.search.textChanged.connect(self._refresh_list)
        if self._mode_list is not None:
            # 貼り付けなどで「予約文字+続き」が一度に入ったときの解釈。
            # 打鍵のほうは eventFilter で先に拾うので、ここは文字列として
            # 入ってきた場合だけが対象。
            self.search.textChanged.connect(self._mode_split_text)
        # 入力欄にフォーカスがある間も ↑↓ / Enter / Esc を効かせたい。QLineEdit が先に
        # 食べてしまうので、イベントフィルタで横取りする。
        self.search.installEventFilter(self)
        layout.addWidget(self.search)

        # モードのチップ。入力欄の左端に重ねる小さい部品(試作 D4 の寸法:
        # 9pt・余白最小・角丸 3px)。文字として打たれるのではない。
        self._chip = None
        if self._mode_list is not None:
            self._chip = QLabel(self.search)
            self._chip.setFont(QFont("Meiryo", 9))
            self._chip.hide()

        self.list = QListWidget()
        self.list.setFont(font)
        # 省略は中央で。パスは末尾のフォルダ名がいちばん大事なので、既定の
        # 末尾省略(先頭しか見えない)ではなく、先頭と末尾を残す形にする。
        self.list.setTextElideMode(Qt.ElideMiddle)
        # 項目間の隙間を殺す。padding(スタイルシート)と別に効くので両方詰める。
        self.list.setSpacing(0)
        # 全項目が同じ高さだと Qt が高さ計算を省ける。行高を測る _fit_to_items とも相性がよい。
        self.list.setUniformItemSizes(True)
        # itemActivated だけを繋ぐ。Windowsではダブルクリックでこれも飛ぶので、
        # itemDoubleClicked と両方繋ぐと _commit が二度走る({input}を含む定型文だと
        # 入力ダイアログが2回出てしまう)。
        self.list.itemActivated.connect(lambda _item: self._commit())
        layout.addWidget(self.list)

        # プレビュー。読み取り専用で、選択が動くたびに中身を差し替える。
        self.preview = None
        if preview_provider is not None:
            self.preview = QPlainTextEdit()
            self.preview.setObjectName("pickerPreview")
            self.preview.setFont(QFont("Meiryo", 9))
            self.preview.setReadOnly(True)
            # 折り返す。もとは「テンプレートは行単位で書かれているので、折り返すと
            # 実際にコピーされる形と見た目がずれる」として折り返さずにいたが、
            # 実測すると長い行は右へ 325px はみ出して**そもそも見えていなかった**。
            # 見えない正確さより、見える近似を採る(1行がどこで終わるかを字面で
            # 確かめたいときは Ctrl+E でファイルを開く)。
            self.preview.setLineWrapMode(QPlainTextEdit.WidgetWidth)
            self.preview.setFixedHeight(self._preview_height)
            layout.addWidget(self.preview)
            # currentItemChanged はクリック・↑↓・絞り込みのどれで動いても飛ぶ。
            # itemSelectionChanged と両方繋ぐと二度走るので、こちらだけにする。
            self.list.currentItemChanged.connect(self._on_current_changed)

        # キーヒント/変数の早見表。高さは行数から固定値で決める(offscreen対策)。
        self.hint = None
        self._hint_height = 0
        if hint:
            self.hint = QLabel(hint)
            self.hint.setObjectName("pickerHint")
            self.hint.setFont(QFont("Meiryo", 8))
            # QLabel は既定(AutoText)だと中身をHTMLとして解釈しうる。早見表には
            # {clipboard} のような記法をそのまま出したいので平文に固定する。
            self.hint.setTextFormat(Qt.PlainText)
            self.hint.setTextInteractionFlags(Qt.NoTextInteraction)
            self._hint_height = HINT_LINE_HEIGHT * (hint.count("\n") + 1)
            self.hint.setFixedHeight(self._hint_height)
            layout.addWidget(self.hint)

        # 見た目は toast / color_picker の情報パネルに揃える。地の色の指定を objectName で
        # 絞っているのは、子として開く QInputDialog までダークに塗り替えないため
        # (ボタンまで背景色を指定するとネイティブの見た目が消える)。プレビューと
        # ヒントを objectName で絞るのも同じ理由で、素の QPlainTextEdit / QLabel を
        # 指定すると QInputDialog.getMultiLineText の入力欄やラベルまで巻き込む。
        self.setObjectName("pickerWindow")
        self.setStyleSheet(
            "#pickerWindow { background-color: #141414; color: #ffffff; }"
            "#pickerWindow QLineEdit { background-color: #222222; color: #ffffff;"
            " border: 1px solid #3c3c3c; border-radius: 4px; padding: 5px; }"
            "QListWidget { background-color: #1c1c1c; color: #ffffff;"
            " border: 1px solid #3c3c3c; border-radius: 4px; }"
            f"QListWidget::item {{ padding: {ITEM_PADDING_V}px {ITEM_PADDING_H}px; }}"
            "QListWidget::item:selected { background-color: #2563eb; }"
            "#pickerPreview { background-color: #1c1c1c; color: #d4d4d4;"
            " border: 1px solid #3c3c3c; border-radius: 4px; padding: 4px; }"
            "#pickerHint { color: #8a8a8a; }"
        )

        self._refresh_list("")
        self._fit_to_items()
        self._move_to_center()

        # 入力ガード(work-board#15): 候補が0件になる編集を無効にして、
        # 入力を直前の状態に戻す。初期状態は「空の入力で全件出ている」。
        # textChanged への接続は _refresh_list の後にする(先に一覧が
        # 組み直り、行数が新しい入力への候補数になるように)。
        self._accepted_text = ""
        self._accepted_count = self.list.count()
        self._rejecting = False
        self.search.textChanged.connect(self._guard_search_text)
        if self._mode_list is not None:
            self._apply_mode_visuals()

    def _guard_search_text(self, text: str) -> None:
        """候補が0件になる入力を無効にする。textChanged の時点で
        _refresh_list は済んでいて、一覧の行数が新しい入力への候補数
        そのもの(動的項目を含む。判定は accepts_input に寄せる)。

        貼り付けは text の差し替えとして一度に来るので、結果が0件なら
        貼り付け全体が戻る。IME の変換中は text() 自体が変わらない
        (未確定文字列は textChanged を出さない)ので判定は走らず、
        確定した時点で確定分がまとめて判定される。"""
        if self._rejecting:
            return
        if self._mode_switching:
            # モード切り替えに伴う書き換えの最中。基準は切り替え側が
            # 合わせ直すので、ここでは判定しない。
            return
        if accepts_input(self._accepted_text, text,
                         self._accepted_count, self.list.count()):
            self._accepted_text = text
            self._accepted_count = self.list.count()
            return
        self._rejecting = True
        try:
            self.search.setText(self._accepted_text)
            self.search.setCursorPosition(len(self._accepted_text))
        finally:
            self._rejecting = False
        self._flash_search_rejected()

    def _flash_search_rejected(self) -> None:
        """無効にした合図。入力欄を一瞬だけ色づける(音は出さない)。"""
        self.search.setStyleSheet(REJECT_FLASH_STYLE)
        QTimer.singleShot(REJECT_FLASH_MS, self._clear_search_flash)

    def _clear_search_flash(self) -> None:
        try:
            self.search.setStyleSheet("")
        except RuntimeError:
            pass   # 色が戻る前に窓が閉じた。合図は済んでいるので問題ない

    # ---------------------------------------------------------------
    # モード切り替え(modes を渡したときだけ)
    # ---------------------------------------------------------------
    def current_mode(self):
        """いまのモードの予約文字。モード無し・モード未対応なら None。"""
        return self._mode

    def _position_chip(self) -> None:
        """チップを入力欄の左端・縦中央に置く(試作 D4 の置き方)。"""
        if self._chip is None or not self._chip.isVisible():
            return
        height = self.search.height()
        y = (height - self._chip.height()) // 2 if height > 0 else 7
        self._chip.move(5, max(y, 0))

    def _apply_mode_visuals(self) -> None:
        """チップとプレースホルダを、いまのモードに合わせる。

        モード名は入力欄の背景に薄く出す(プレースホルダの仕組みそのまま。
        入力が始まれば Qt が消す)。モード無しでは呼び出し側のプレースホルダ
        に戻す。"""
        if self._chip is None:
            return
        if self._mode is None:
            self._chip.hide()
            self.search.setTextMargins(0, 0, 0, 0)
            self.search.setPlaceholderText(self._placeholder_base)
            return
        name, color = self._mode_map[self._mode]
        self._chip.setText(self._mode)
        self._chip.setStyleSheet(
            f"background: {color}; color: white; border-radius: 3px;"
            " padding: 0px 2px; font-weight: bold;")
        self._chip.adjustSize()
        self._chip.show()
        self._position_chip()
        self.search.setTextMargins(self._chip.width() + 7, 0, 0, 0)
        self.search.setPlaceholderText(f"{name}モード")

    def _mode_key_press(self, event) -> bool:
        """予約文字の打鍵と、空入力での Backspace(チップ外し)を拾う。

        予約文字が効くのは入力が空のときだけ。文字が入っているときの
        `[` などは、ただの文字として通す(パスや検索語の一部でありうる)。"""
        if event.key() == Qt.Key_Backspace and not self.search.text() \
                and self._mode is not None:
            self._enter_mode(None)
            return True
        text = event.text()
        if len(text) == 1 and text in self._mode_map \
                and not self.search.text():
            self._enter_mode(text)
            return True
        return False

    def _enter_mode(self, char) -> None:
        """モードを切り替える。候補の差し替えは on_mode_change 側の役目。

        入力は空に戻す(モードを決めるための文字・絞り込みの残りは、
        新しいモードの入力ではないため)。`=` は電卓行きで、この窓は閉じる。"""
        if char == "=" and self._on_calc is not None:
            self._enter_calc("")
            return
        self._mode = char
        self._mode_switching = True
        try:
            self.search.setText("")
        finally:
            self._mode_switching = False
        self._apply_mode_visuals()
        if self._on_mode_change is not None:
            self._on_mode_change(char)
        if char is None or self._on_mode_change is None:
            # モード無しでは、候補ではなく切替ヒント行が本体。コールバック側は
            # モード無し用の候補を持たないので、ここで必ず描き直す。
            self._refresh_list("")

    def _enter_calc(self, expr: str) -> None:
        """`=` が選ばれた。ピッカーは閉じて、続きは電卓の窓に任せる。"""
        if self._on_calc is None:
            return
        self.close()
        self._on_calc(expr)

    def _mode_split_text(self, text: str) -> None:
        """文字列として入ってきた先頭の予約文字をモードとして解釈する。

        貼り付けや setText で「[そら」の形が一度に来たとき用。打鍵は
        _mode_key_press が先に拾うので、ここに来るのは文字列の経路だけ。
        モードが決まっている間の先頭文字は解釈しない(ただの文字)。"""
        if self._mode_switching or self._mode is not None or not text:
            return
        char = text[0]
        if char not in self._mode_map:
            return
        rest = text[1:]
        if char == "=" and self._on_calc is not None:
            self._enter_calc(rest)
            return
        self._mode = char
        self._mode_switching = True
        try:
            self.search.setText(rest)
        finally:
            self._mode_switching = False
        # 入力ガードの基準を、切り替え後の入力と行数に合わせ直す
        # (ガードは _mode_switching の間は判定しない取り決め)。
        self._accepted_text = self.search.text()
        self._accepted_count = self.list.count()
        self._apply_mode_visuals()
        if self._on_mode_change is not None:
            self._on_mode_change(char)

    def _fit_to_items(self):
        """項目数に合わせて高さを決める。

        絞り込みのたびには変えない。1文字打つごとにウインドウが伸び縮みすると
        目が落ち着かないうえ、中央配置なので位置まで動いてしまう。"""
        # 項目が無いと -1 が返る。環境によっては極端に小さい値を返すこともあるので、
        # 「フォントの高さ + スタイルシートの上下padding」を下限として噛ませる。
        row_height = max(
            self.list.sizeHintForRow(0),
            self.list.fontMetrics().height() + ITEM_PADDING_V * 2,
        )

        max_rows = MAX_VISIBLE_ROWS if self.preview is None else PREVIEW_MAX_VISIBLE_ROWS
        rows = min(max(len(self._items), MIN_VISIBLE_ROWS), max_rows)

        spacing = self.layout().spacing()
        margins = self.layout().contentsMargins()
        height = (
            self.search.sizeHint().height()
            + spacing
            + row_height * rows
            + self.list.frameWidth() * 2
            + margins.top()
            + margins.bottom()
        )
        # 追加分は測らずに固定値で足す。offscreen では sizeHint が当てにならず、
        # 測った値を信じるとプレビューが潰れた高さでウインドウが決まってしまう。
        if self.preview is not None:
            height += spacing + self._preview_height
        if self.hint is not None:
            height += spacing + self._hint_height

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        height = min(height, int(screen.availableGeometry().height() * MAX_SCREEN_RATIO))
        width = WINDOW_WIDTH if self.preview is None else PREVIEW_WINDOW_WIDTH
        # 幅は中身に合わせる: いちばん長い項目が省略なしで収まる幅までは広げる。
        # 下限は従来の固定幅(短い一覧では広げない)、上限は画面幅の MAX_WIDTH_RATIO
        # で、それでも収まらない分は一覧の中央省略が末尾を見せる。
        metrics = self.list.fontMetrics()
        longest = max((metrics.horizontalAdvance(entry[0]) for entry in self._items),
                      default=0)
        needed = (longest + ITEM_PADDING_H * 2 + self.list.frameWidth() * 2
                  + margins.left() + margins.right())
        if len(self._items) > rows:
            needed += self.list.verticalScrollBar().sizeHint().width()
        limit = int(screen.availableGeometry().width() * MAX_WIDTH_RATIO)
        width = max(width, min(needed, limit))
        self.resize(width, height)

    def _move_to_center(self):
        """カーソルのある画面の中央よりやや上に出す(toastと同じ理由でprimaryScreen固定にしない)。

        VERTICAL_OFFSET のぶん持ち上げるが、持ち上げすぎて画面の外に出ないよう
        作業領域に収まる範囲でクランプする(タスクバーの下に潜らせない)。"""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        center = area.center()

        x = center.x() - self.width() // 2
        y = center.y() - self.height() // 2 + VERTICAL_OFFSET

        x = max(area.left(), min(x, area.right() - self.width()))
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        self._position_chip()
        # 常駐アプリのメニューやホットキーから開くとフォーカスが来ないことがある。
        # 入力欄まで明示的にフォーカスを渡さないと、そのまま打った文字が前のアプリへ飛ぶ。
        self.raise_()
        self.activateWindow()
        self.search.setFocus()
        # 取れていなければ、もう一度だけ自分で前面を取りにいく。
        #
        # 【なぜ要るか】
        # Windows の前面化ロックが効いていると(実測: ForegroundLockTimeout が
        # 2147483647ms ＝実質無期限)、前面でないプロセスからの SetForegroundWindow は
        # 黙って失敗する。activateWindow() の中身がこれ。さらにこの窓は常に最前面に
        # 描かれる設定なので、**フォーカスが来なくても見た目には開いている**。
        # 打った文字が前のアプリへ飛ぶ、といういちばん困る形になる。
        #
        # あふｗから開く経路では、呼び出し側(traytools_send)が前面化の許可を先に
        # 渡している。許可があれば下の呼び出しは通る。許可が無い経路(ホットキー等)
        # では通らないが、そのときは今までと同じ状態に戻るだけで、悪化はしない。
        QTimer.singleShot(0, self._ensure_foreground)

    def _ensure_foreground(self):
        """前面が自分でなければ、明示的に取りにいく。取れなければ黙って諦める。"""
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            if user32.GetForegroundWindow() == hwnd:
                return
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            user32.SetForegroundWindow.restype = ctypes.c_bool
            if not user32.SetForegroundWindow(ctypes.c_void_p(hwnd)):
                # 取れなかった。窓は見えているので操作はできるが、打った文字は
                # 前のアプリへ行く。原因を追えるように記録だけ残す。
                print("[picker] 前面を取れませんでした（前面化ロック）",
                      file=sys.stderr)
                return
            self.activateWindow()
            self.search.setFocus()
        except Exception as e:  # noqa: BLE001  フォーカスのために窓を落とさない
            print(f"[picker] 前面化に失敗: {e}", file=sys.stderr)

    def closeEvent(self, event):
        super().closeEvent(event)
        # close() は複数の経路(Esc・決定後・呼び出し側)から呼ばれる。closed を受けた側が
        # また close() することもあるので、通知は1回だけに絞る。
        if not self._closed_emitted:
            self._closed_emitted = True
            self.closed.emit()

    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == QEvent.KeyPress:
            if self._mode_list is not None and self._mode_key_press(event):
                return True
            if self._handle_shortcut(event):
                return True
            key = event.key()
            if key == Qt.Key_Tab and self._handle_tab():
                return True
            if key in (Qt.Key_Up, Qt.Key_Down):
                self._move_selection(-1 if key == Qt.Key_Up else 1)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._commit()
                return True
            if key == Qt.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        # 入力欄以外(一覧やプレビュー)にフォーカスが移っていても効かせる。
        if self._handle_shortcut(event):
            return
        if event.key() == Qt.Key_Tab and self._handle_tab():
            return
        if event.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def _handle_shortcut(self, event) -> bool:
        """Ctrl+N / Ctrl+E / Ctrl+O / Ctrl+D / Ctrl+↑↓ を処理したら True を返す。

        渡されなかった処理のキーは素通しする(フォルダブックマークのように編集手段を
        持たない呼び出し側で、押しても何も起きない代わりに黙って握り潰さないため)。
        Ctrl+↑↓ も同じで、on_move を渡していない窓では素の ↑↓ として扱われる。"""
        if not (event.modifiers() & Qt.ControlModifier):
            return False
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter) \
                and self._on_ctrl_enter is not None:
            # Ctrl+Enter は「いまの行に追加のメニューを重ねる」ための口。
            # 窓は閉じず、行の解釈と見せ方は呼び出し側が持つ。動的項目でも
            # 呼ぶ(ファイル検索の結果など、実体がパスとして読める行がある)。
            entry = self._entry_at(self.list.currentItem())
            if entry is not None:
                name, data = entry
                try:
                    self._on_ctrl_enter(name, data)
                except Exception as e:  # noqa: BLE001  窓を落とさない
                    print(f"[tray-tools] Ctrl+Enter の処理に失敗しました"
                          f" ({name}): {e}", file=sys.stderr)
            return True
        if key == Qt.Key_N and self._on_new is not None:
            self._run_action(lambda: self._on_new())
            return True
        if key == Qt.Key_E and self._on_edit is not None:
            item = self.list.currentItem()
            index = None if item is None else item.data(Qt.UserRole)
            if isinstance(index, int) and index < 0:
                # 入力から作った項目。設定ファイルに実体が無いので編集できない。
                # 窓は閉じずに何もしない(打ち直しの途中で閉じられると使いにくい)。
                return True
            entry = self._entry_at(item)
            if entry is None:
                return True  # 選択が無いだけ。開くものが無いので何もしない。
            name, data = entry
            self._run_action(lambda: self._on_edit(name, data))
            return True
        if key == Qt.Key_O and self._on_open_folder is not None:
            self._run_action(lambda: self._on_open_folder())
            return True
        if key == Qt.Key_D and self._on_delete is not None:
            self._run_on_selection(self._on_delete)
            return True
        if key in (Qt.Key_Up, Qt.Key_Down) and self._on_move is not None:
            step = -1 if key == Qt.Key_Up else 1
            self._run_on_selection(
                lambda name, data: self._on_move(name, data, step))
            return True
        return False

    def _run_on_selection(self, func) -> None:
        """選択中の項目で func(表示名, データ) を呼ぶ。窓は閉じない。

        _run_action と違って閉じないのは、削除・並べ替えが「続けて何度もやる」操作
        だから。一覧の組み直しは呼び出し側が set_items でやる。

        動的項目(入力から生えた行)では呼ばない。設定ファイルに実体が無く、消すことも
        動かすこともできない。キーを食べるかどうかは呼び出し元が決める(ここまで来た
        時点で処理は渡されているので、素通しさせるとフォーカスが飛んでしまう)。"""
        if self._committing:
            return
        item = self.list.currentItem()
        index = None if item is None else item.data(Qt.UserRole)
        if isinstance(index, int) and index < 0:
            return
        entry = self._entry_at(item)
        if entry is None:
            return
        name, data = entry
        try:
            func(name, data)
        except Exception as e:
            # Qtのスロット内で投げ切ると常駐ごと落ちる。窓は生かしたまま記録に残す。
            print(f"[tray-tools] ピッカーの操作に失敗しました ({name}): {e}", file=sys.stderr)

    def _handle_tab(self) -> bool:
        """Tab を処理したら True(＝Qtに渡さない)。tab_text を渡されていなければ False。

        Qt の既定では Tab はフォーカス移動なので、入力欄の eventFilter と一覧側の
        keyPressEvent の両方で横取りしないと取りこぼす。tab_text がある窓では、
        差し込む文字列が無い(None)ときも食べてしまう。入力欄からフォーカスが飛ぶと
        そのまま打ち続けられなくなり、Tabを押しただけで操作不能に見えるため。

        入力欄を書き換えるだけで、一覧の組み直しは textChanged に任せる。"""
        if self._tab_text is None:
            return False
        entry = self._entry_at(self.list.currentItem())
        if entry is None:
            return True
        name, data = entry
        try:
            text = self._tab_text(name, data)
        except Exception as e:
            # Tabが効かないだけで済ませる。Qtのスロット内で投げ切ると常駐ごと落ちる。
            print(f"[tray-tools] Tabの行き先を決められません: {e}", file=sys.stderr)
            return True
        if not text:
            return True
        self.search.setText(text)
        self.search.setCursorPosition(len(text))
        return True

    def _run_action(self, func) -> None:
        """Ctrl+N/E/O の処理を走らせて、このウインドウを閉じる。

        _commit と同じく例外はここで止める(Qtのスロット内で投げ切ると常駐ごと落ちる)。
        閉じるのは、外部エディタで直した結果を出すには開き直すしかなく、開いたままだと
        古い一覧が残って食い違うため。"""
        if self._committing:
            return
        self._committing = True
        try:
            func()
        except Exception as e:
            print(f"[tray-tools] ピッカーの操作に失敗しました: {e}", file=sys.stderr)
        finally:
            self.close()

    def _on_current_changed(self, current, _previous):
        """選択が動くたびにプレビューを差し替える。preview_provider を渡した時だけ繋がる。"""
        entry = self._entry_at(current)
        if entry is None:
            self.preview.setPlainText("")
            return
        _name, data = entry
        try:
            self.preview.setPlainText(self._preview_provider(data) or "")
        except Exception as e:
            # プレビューが作れないだけで選べなくなるのは行き過ぎ。理由は残しつつ続ける。
            print(f"[tray-tools] プレビューを作れません: {e}", file=sys.stderr)
            self.preview.setPlainText("(プレビューを作れませんでした)")

    def _entry_at(self, item):
        """一覧の項目から (表示名, データ) を取り出す。取れなければ None。

        0 以上は self._items の添字、負数は dynamic_items から作った項目(i 番目が
        -(i + 1))。self._items[-1] は「最後の要素」として黙って通ってしまい、Enter の
        行き先が別物になるので、引く側は必ずここを通す。動的項目の側も同じ厳しさで
        範囲を見る(一覧を組み直した直後に古い項目を引かされても取り違えないため)。"""
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        if not isinstance(index, int):
            return None
        if index < 0:
            dynamic_index = -index - 1
            if not (0 <= dynamic_index < len(self._dynamic)):
                return None
            return self._dynamic[dynamic_index]
        if not (index < len(self._items)):
            return None
        name, data, _match = split_item(self._items[index])
        return (name, data)

    def _build_dynamic(self, text: str) -> list:
        """dynamic_items を呼んで項目の一覧を作る。作れなければ空リスト。

        この関数は1文字打つごとに呼ばれる。ここで例外を投げ切ると textChanged の
        スロットの中で落ちることになり、打鍵そのものができなくなる。preview_provider と
        同じく、理由だけ残して「項目なし」として続ける。"""
        if self._dynamic_items is None:
            return []
        try:
            entries = self._dynamic_items(text)
        except Exception as e:
            print(f"[tray-tools] 入力からの項目を作れません: {e}", file=sys.stderr)
            return []
        if not entries:
            return []
        built = []
        try:
            for entry in entries:
                name, data = entry
                built.append((name, data))
        except (TypeError, ValueError) as e:
            # 途中まで組めていても捨てる。半端な一覧を出すより出さない方が分かりやすい。
            print(f"[tray-tools] 入力からの項目の形が違います: {e}", file=sys.stderr)
            return []
        return built

    def _refresh_list(self, text: str):
        """前方一致(大文字小文字を無視)で候補そのものを減らす。絞り込んだ結果が
        見た目に残っていると ↑↓ で選べてしまい、Enterの行き先が分からなくなる。

        dynamic_items がある場合、そこから作った項目を返ってきた順に置く。
        既定は先頭(初期選択は先頭行なので、打った直後に Enter を押せばその
        1件目が選ばれる)。dynamic_last=True のときは静的候補の後ろに置く
        (登録など常設の候補を上に残したい窓用)。"""
        keyword = text.strip().lower()
        self.list.clear()
        if self._mode_list is not None and self._mode is None:
            # モード無しの間は、候補ではなくモード切り替えのヒント行を出す。
            # 行の UserRole には "mode:<文字>" を持たせ、_commit で拾う。
            # 絞り込みはモード名への前方一致(他の候補と同じ判定)。
            self._dynamic = []
            for char, name, _color in self._mode_list:
                label = f"{char} {name}モード"
                if not name_matches(f"{name}モード", keyword):
                    continue
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, f"mode:{char}")
                self.list.addItem(item)
            if self.list.count():
                self.list.setCurrentRow(0)
            return
        self._dynamic = self._build_dynamic(text)

        def add_dynamic():
            for dynamic_index, (name, _data) in enumerate(self._dynamic):
                item = QListWidgetItem(name)
                item.setData(Qt.UserRole, dynamic_role(dynamic_index))
                self.list.addItem(item)

        def add_static():
            for index, entry in enumerate(self._items):
                name, _data, match = split_item(entry)
                if not name_matches(match, keyword):
                    continue
                item = QListWidgetItem(name)
                # データそのものではなく元リストの添字を持たせる。Qtの項目データは
                # QVariant を経由するため、タプルなどはPython側の型が変わって戻ることがある。
                item.setData(Qt.UserRole, index)
                self.list.addItem(item)

        if self._dynamic_last:
            add_static()
            add_dynamic()
        else:
            add_dynamic()
            add_static()
        if self.list.count():
            self.list.setCurrentRow(0)

    def set_items(self, items: list) -> None:
        """一覧の元データを差し替えて組み直す。選択行はできるだけ保つ。

        Ctrl+D/Ctrl+↑↓ のように「窓を開けたまま一覧が変わる」操作のための口。
        組み直すと選択は先頭に戻ってしまい、2件目を消す・何段も動かすたびに
        選び直すことになるので、ここで面倒を見る。

        選び方は2段構え。まず、選んでいたものと同じ (表示名, データ) が新しい一覧に
        あればそれを選ぶ(並べ替えで行が動いても同じ項目を追いかける)。無ければ
        同じ行番号に留まる(消したときは、その場所に繰り上がってきたものが選ばれる。
        最後の行を消したときは行数に合わせて1つ上になる)。どちらも当てはまらない
        (空になった)なら選択なし。"""
        previous = self._entry_at(self.list.currentItem())
        row = self.list.currentRow()

        self._items = list(items)
        self._refresh_list(self.search.text())
        # 項目の差し替えは打ち込みではないので、入力ガードの基準も
        # 今の入力と行数に合わせ直す(古い行数のままだと、次の打鍵の
        # 判定がずれる)。
        self._accepted_text = self.search.text()
        self._accepted_count = self.list.count()

        count = self.list.count()
        if count == 0:
            return
        target = -1
        if previous is not None:
            for index in range(count):
                if self._entry_at(self.list.item(index)) == previous:
                    target = index
                    break
        if target < 0:
            target = min(max(row, 0), count - 1)
        self.list.setCurrentRow(target)

    def _move_selection(self, step: int):
        count = self.list.count()
        if count == 0:
            return
        row = (self.list.currentRow() + step) % count
        self.list.setCurrentRow(row)

    def _commit(self):
        if self._committing:
            return
        current = self.list.currentItem()
        if current is not None:
            role = current.data(Qt.UserRole)
            if isinstance(role, str) and role.startswith("mode:"):
                # モード切り替えのヒント行。決定ではなくモードへの移行。
                self._enter_mode(role[len("mode:"):])
                return
        entry = self._entry_at(current)
        if entry is None:
            return
        self._committing = True
        name, data = entry
        keep_open = False
        try:
            # 閉じる前に呼ぶ。on_accept が QInputDialog を出す場合、まだ生きている
            # このウインドウを親にできる(閉じた後だとダイアログの位置と前面化が怪しくなる)。
            # True を返したら窓を閉じない(「登録済みです」でその行を選んで見せるとき)。
            keep_open = self._on_accept(name, data) is True
        except Exception as e:
            # Qtのスロット内で例外を投げ切るとアプリごと落ちうる。1件の失敗で常駐を
            # 巻き添えにしないよう、ここで止めて標準エラーに残す。
            print(f"[tray-tools] 選択の処理に失敗しました ({name}): {e}", file=sys.stderr)
        finally:
            if keep_open:
                self._committing = False
            else:
                self.close()

    def select_data(self, data, name=None) -> bool:
        """data(と name)が一致する行を選ぶ。絞り込みで隠れていれば絞り込みを解いて探す。"""
        def find() -> int:
            for row in range(self.list.count()):
                entry = self._entry_at(self.list.item(row))
                if entry is not None and entry[1] == data and (name is None or entry[0] == name):
                    return row
            return -1

        row = find()
        if row < 0 and self.search.text():
            self.search.clear()
            self._refresh_list("")
            row = find()
        if row < 0:
            return False
        self.list.setCurrentRow(row)
        return True
