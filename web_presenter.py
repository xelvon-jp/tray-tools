# web_presenter.py
# 任意のウェブサイトを取り込んで、同梱の発表者ツール(presenter.html)に読ませる。
#
# なぜこれが要るか:
#   presenter.html は資料を about:blank へ document.write して「親と同一オリジン」に
#   することで、スライドの検出・送り・次スライドのプレビューを実現している。だから
#   資料は「HTML の文字列」でなければならず、他所のサイトを iframe で直接開くと
#   クロスオリジンで DOM に触れず何もできない。
#
#   そこで、こちらがブラウザ(QtWebEngine)を持つ。サイトをこちらで開き、レンダリング後の
#   DOM を文字列として取り出して presenter.html に渡す。相手のサーバから見れば普通の
#   閲覧なので壁は無い。取り出した後はただの HTML 文字列なので、presenter.html は
#   ローカル資料と区別せずに扱える(=あちらは1バイトも変えていない)。
#
# presenter.html を改造しない理由:
#   「単一ファイルを file:// からダブルクリックで動かせる」ことがあのファイルの
#   存在理由なので、こちらの都合を持ち込まない。代わりに、あのファイルの内容の末尾へ
#   「起動時に loadHtml() を呼ぶだけの <script>」を足したものを %TEMP% に書き出し、
#   既定のブラウザに投げる。loadHtml(html, name) はグローバル関数で、あちらの
#   <script> は module ではないので窓から見える。
#
# QtWebEngine は PySide6 のフルインストールに最初から入っているので追加依存はゼロ。
# ただし読み込みは重いので、import は関数の中で行う(常駐の起動時間に乗せない)。
# 実測(このPC): 遅延 import 45ms、example.com を開いて toHtml() まで含めて 0.35 秒。
#
# 窓は出さない。QWebEngineView は show() せずに使う(Chromium は非表示でも読み込みと
# スクリプト実行を行う)。そのぶん「WebEngine の中で対話的にログインする」ことは
# できないので、認証の要るサイトは取り込めない(README の制約を参照)。
import json
import os
import re
import sys
import tempfile
import time
from html import escape
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtWidgets import QInputDialog, QLineEdit

import settings as settings_module

# 書き出し先。資料フォルダは汚さない(<base> で絶対 URL 解決になるので、置き場所は
# どこでもよい)。capture_process.HANDOFF_DIR と同じく %TEMP% の下に自分の部屋を作る。
TEMP_DIR = Path(tempfile.gettempdir()) / "traytools-presenter"
TEMP_GLOB = "site_*.html"

# 取り残しを片付けるまでの猶予(秒)。付箋の受け渡し PNG(5分)よりずっと長くしてある。
# あちらは「開いた瞬間に読まれて消える」ファイルだが、こちらはブラウザのタブが
# 生きている限り再読み込み(F5)の対象で、消すとタブが白くなる。発表1回ぶんは残す。
TEMP_STALE_SECONDS = 6 * 3600

# 読み込みの打ち切り(秒)。loadFinished が永遠に来ないサイトが実在するので必須。
# 30秒は「重いページでも大抵は載る」と「待たされて不安になる手前」の折り合い。
DEFAULT_TIMEOUT_SECONDS = 30

# loadFinished の後、DOM を取り出すまでの追加の待ち(ms)。load 直後に走る JS が
# 中身を書き足すページが多く、すぐ toHtml() すると骨組みだけを掴むことがある。
DEFAULT_SETTLE_MS = 800

# 入力欄に出す履歴の件数。増やしても選びにくくなるだけなので控えめに。
RECENT_MAX = 10

DIALOG_TITLE = "サイトを取り込んで開く"

# URL 入力ダイアログに足す窓のフラグ。Qt 側で Qt.Dialog と OR されるので、これは
# 「足すぶん」だけを書く。
DIALOG_FLAGS = Qt.WindowStaysOnTopHint

# 動いている取り込みを掴んでおく。読み込みは非同期(loadFinished)なので、ここで
# 参照を持たないと関数を抜けた瞬間に GC が QWebEngineView を消して何も起きない。
_ACTIVE = set()


def _int_setting(value, default: int) -> int:
    """設定値を正の整数にする。数として読めない/0以下なら既定へ落とす
    (feature_screen._positive_number と同じ作法・同じ理由)。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


# ---------------------------------------------------------------
# URL の入力
# ---------------------------------------------------------------
def normalize_url(text: str):
    """入力された文字列を URL にする。URL として扱えなければ None。

    「example.com」のようにスキーマを省いて書かれることが多いので https:// を補う。
    ローカルのパス(C:\\... や \\\\server\\...)を貼られたら file:// に直す
    (それは従来の「📽 発表者ツール」の仕事だが、弾くより開けるほうが親切)。"""
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return None
    if text.startswith("\\\\") or re.match(r"^[A-Za-z]:[\\/]", text):
        return QUrl.fromLocalFile(text).toString()
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", text):
        text = "https://" + text
    url = QUrl(text)
    if not url.isValid():
        return None
    scheme = url.scheme().lower()
    if scheme not in ("http", "https", "file"):
        return None
    if scheme in ("http", "https") and not url.host():
        return None
    return url.toString()


def _recent_urls(app_settings: dict) -> list:
    raw = (app_settings.get("tools") or {}).get("web_presenter_recent")
    if not isinstance(raw, list):
        return []
    out = []
    for value in raw:
        if isinstance(value, str) and value.strip() and value not in out:
            out.append(value.strip())
    return out[:RECENT_MAX]


def ask_url(parent, app_settings: dict):
    """取り込む URL を尋ねる。キャンセル/空なら None。

    picker.py(定型文・フォルダブックマークの選択ウインドウ)は流用しない。あれは
    「用意された一覧から1つ選ぶ」ための窓で、自由入力の口を持たない。ここで欲しいのは
    毎回違う文字列の入力なので、素直に QInputDialog を使う。ただし「前と同じサイトを
    もう一度取り込む」は頻繁に起きるため、履歴があるときは編集可能なコンボボックス
    (getItem の editable=True)にして、選ぶことも打ち直すこともできるようにする。

    最前面指定を付けるのは、呼び元がトレイメニューとタスクバーウィジェットの2つあり、
    後者は枠なし・最前面の窓だから。素の親なしダイアログだとその陰に出て「押したのに
    何も起きない」ように見える(実際は入力待ちで止まっている)。"""
    label = "取り込む URL（例: https://example.com/）"
    recent = _recent_urls(app_settings)
    if recent:
        text, ok = QInputDialog.getItem(
            parent, DIALOG_TITLE, label, recent, 0, True, DIALOG_FLAGS
        )
    else:
        text, ok = QInputDialog.getText(
            parent, DIALOG_TITLE, label, QLineEdit.Normal, "", DIALOG_FLAGS
        )
    if not ok:
        return None
    return normalize_url(text)


def remember_url(app_settings: dict, settings_path, url: str) -> bool:
    """取り込みに成功した URL を履歴の先頭へ入れて保存する。成否を返す。

    メモリ上の app_settings は既定値をマージ済みなので、それを丸ごと書き出すと
    未設定の既定値まで明示的に書かれてファイルの姿が変わる。launcher.save_bookmark と
    同じく、ファイルを読み直して tools.web_presenter_recent だけを差し替える。"""
    recent = [u for u in _recent_urls(app_settings) if u != url]
    recent.insert(0, url)
    recent = recent[:RECENT_MAX]

    tools = app_settings.setdefault("tools", {})
    if not isinstance(tools, dict):
        tools = app_settings["tools"] = {}
    tools["web_presenter_recent"] = recent

    if not settings_path:
        return False
    try:
        stored = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
        stored_tools = stored.get("tools")
        if not isinstance(stored_tools, dict):
            stored_tools = stored["tools"] = {}
        stored_tools["web_presenter_recent"] = recent
        settings_module.save_settings(stored, settings_path)
        return True
    except (OSError, ValueError, TypeError, AttributeError) as e:
        print(f"[tray-tools] 取り込み履歴を保存できません: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------
# 取り込んだ HTML の加工
# ---------------------------------------------------------------
_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HTML_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_BASE_RE = re.compile(r"<base\b[^>]*?>", re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)


def inject_base(html: str, base_url: str) -> str:
    """<head> の先頭に <base href="元のURL"> を挿す。

    これだけで、資料の中の相対参照(画像・CSS・フォント)が元のサーバから引ける。
    presenter.html の resolveAssets() は App.assets が空なら HTML を素通しするので、
    URL 取り込みでは資産の登録は要らない。

    元からある <base> は消す。HTML では文書順で最初の <base href> だけが効くため、
    残したまま前に足しても相手が勝つとは限らず、しかも相手が相対 href だと %TEMP% の
    file:// を基準に解決されて全部壊れる。相手が絶対 href だった場合の値は
    document.baseURI として拾ってあるので(こちらが挿す href がそれ)、情報は落ちない。

    削除は <head> の中だけに限る。本文側の <script> 文字列などに現れる "<base" まで
    消さないため。"""
    head_end = html.lower().find("</head>")
    if head_end >= 0:
        html = _BASE_RE.sub("", html[:head_end]) + html[head_end:]
    else:
        html = _BASE_RE.sub("", html)

    tag = '<base href="%s">' % escape(base_url or "", quote=True)
    m = _HEAD_RE.search(html)
    if m:
        return html[: m.end()] + tag + html[m.end():]
    m = _HTML_RE.search(html)
    if m:
        return html[: m.end()] + "<head>" + tag + "</head>" + html[m.end():]
    return tag + html


def strip_scripts(html: str) -> str:
    """<script> をすべて落とす。設定 tools.web_presenter_strip_scripts 用の逃げ道。

    取り込んだ HTML は presenter.html の iframe の中で改めて実行される。ページによっては
    その JS が DOM を作り直したり、元の URL 前提の処理で例外を投げて表示が崩れる。
    そういうサイトを「静止画としてなら使える」状態に落とすための切り替え。既定は
    落とさない(reveal.js のようにスライド送りを JS が担う資料もあるため)。

    最短一致にしてあるのは、HTML パーサ自身が「最初に現れた </script で script 要素を
    閉じる」ためで、JS の文字列リテラルの中かどうかは見ない。同じ切り方に揃えておけば、
    残る文字列もブラウザが本文として扱うぶんと一致する。"""
    return _SCRIPT_RE.sub("", html)


def _js_string(text: str) -> str:
    """文字列を JavaScript のリテラル(引用符込み)にする。

    json.dumps だけでは足りない。取り込んだ資料の中に "</script>" が現れると、
    そこで囲っている <script> タグが閉じてしまい、以降が本文として垂れ流される
    (HTML パーサは JS の文字列リテラルの中かどうかを見ない)。'<' をすべて \\u003c に
    逃がせば "</script>" も "<!--" も HTML パーサからは見えなくなる。
    ensure_ascii=True の出力に生の '<' が現れるのは元の文字としてだけなので、
    この一括置換で壊れるものは無い。"""
    return json.dumps(text, ensure_ascii=True).replace("<", "\\u003c")


# 末尾に足すブートストラップ。presenter.html 本体の <script> は末尾で init() を
# 呼び終えているので、その後ろに置けば loadHtml は既に定義済み・DOM も完成している。
_BOOTSTRAP = """<script>
/* tray-tools: 取り込んだサイトを起動時に読ませる（presenter.html 本体は無改造） */
(function () {
  var src = %s;
  var name = %s;
  function go() {
    try {
      loadHtml(src, name);
    } catch (e) {
      console.error('[tray-tools] 取り込んだHTMLを読み込めませんでした', e);
    }
  }
  if (typeof loadHtml === 'function') { go(); } else { window.addEventListener('load', go); }
})();
</script>
"""


def build_page(presenter_path, captured_html: str, base_url: str, name: str,
               strip_js: bool = False) -> str:
    """presenter.html の中身＋ブートストラップの文字列を作る。

    presenter.html は newline='' で読む。universal newlines で読んで書き戻すと
    改行が置き換わり、原本と別物のファイルを配ることになる(このプロジェクトは LF)。"""
    with open(presenter_path, "r", encoding="utf-8", newline="") as f:
        base_page = f.read()

    if strip_js:
        captured_html = strip_scripts(captured_html)
    captured_html = inject_base(captured_html, base_url)

    bootstrap = _BOOTSTRAP % (_js_string(captured_html), _js_string(name or base_url or ""))

    lower = base_page.lower()
    at = lower.rfind("</body>")
    if at < 0:
        return base_page + bootstrap
    return base_page[:at] + bootstrap + base_page[at:]


def sweep_stale_pages() -> None:
    """取り残された取り込み済みページを片付ける。

    毎回 %TEMP% に1枚ずつ増えるので、誰かが消さないと溜まり続ける。ブラウザに投げた
    後は誰も後始末をしないため、次に取り込むついでにここで掃除する
    (capture_process._sweep_stale_handoffs と同じ作法)。"""
    try:
        stale = list(TEMP_DIR.glob(TEMP_GLOB))
    except OSError:
        return
    cutoff = time.time() - TEMP_STALE_SECONDS
    for path in stale:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass  # 開かれている等で消せないものは次の機会に


def write_temp_page(text: str) -> Path:
    """作ったページを %TEMP% に書き出してパスを返す。書けなければ OSError。"""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # 同じミリ秒に2回取り込んでも衝突しないよう pid と ns を混ぜる(付箋の受け渡しと同じ)。
    path = TEMP_DIR / f"site_{os.getpid()}_{time.time_ns()}.html"
    # newline='' を必ず付ける。既定では '\n' が '\r\n' に化け、presenter.html の原本と
    # 改行が変わってしまう。
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


# ---------------------------------------------------------------
# サイトの取り込み本体
# ---------------------------------------------------------------
class SiteCapture(QObject):
    """URL を1つ、非表示の QWebEngineView で開いてレンダリング後の DOM を取り出す。

    使い方は capture() 経由。成功なら on_ready(html, base_url, title)、失敗なら
    on_failed(理由の文字列) がそれぞれ1回だけ呼ばれる(両方呼ばれることはない)。

    自分自身を _ACTIVE に入れて生き延びる。読み込みは非同期なので、呼び出し側の
    ローカル変数だけに頼ると GC に消される。"""

    def __init__(self, url: str, on_ready, on_failed,
                 timeout_ms: int, settle_ms: int):
        super().__init__()
        # ここで初めて QtWebEngine を読み込む。常駐の起動時にこの重さを払わないため。
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self._url = url
        self._on_ready = on_ready
        self._on_failed = on_failed
        self._settle_ms = settle_ms
        self._done = False

        # show() しない。画面に窓を出さずに読み込みと JS 実行だけをさせる。
        self._view = QWebEngineView()

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.setInterval(timeout_ms)
        self._timeout.timeout.connect(self._on_timeout)

    def start(self) -> None:
        _ACTIVE.add(self)
        try:
            self._view.loadFinished.connect(self._on_load_finished)
            # 描画プロセスが落ちると loadFinished は来ない。捕まえないと、タイムアウトが
            # 切れるまで黙って待つことになる。
            self._view.page().renderProcessTerminated.connect(self._on_render_gone)
            self._timeout.start()
            self._view.load(QUrl(self._url))
        except Exception as e:
            # PySide6 はスロットから例外が抜けると常駐アプリごと落ちる。ここは
            # スロットではないが、始末は同じ形に揃えておく。
            self._fail(f"読み込みを開始できませんでした（{type(e).__name__}）")

    # -- スロット(いずれも例外を外へ出さないこと) --------------------
    def _on_load_finished(self, ok: bool) -> None:
        try:
            if self._done:
                return
            if not ok:
                self._fail("読み込めませんでした（URL とネットワークを確認してください）")
                return
            # load 直後に中身を書き足すページが多いので、少し待ってから取り出す。
            QTimer.singleShot(self._settle_ms, self._grab)
        except Exception as e:
            self._fail(f"読み込みに失敗しました（{type(e).__name__}）")

    def _on_render_gone(self, status, exit_code) -> None:
        try:
            self._fail(f"ブラウザエンジンが停止しました（{int(exit_code)}）")
        except Exception:
            self._fail("ブラウザエンジンが停止しました")

    def _on_timeout(self) -> None:
        try:
            # 「%g」で 30 は "30"、1.5 は "1.5" になる(末尾の .0 を出さない)。
            self._fail("時間内に読み込めませんでした（%g秒）" % (self._timeout.interval() / 1000))
        except Exception:
            self._fail("時間内に読み込めませんでした")

    def _grab(self) -> None:
        """document.baseURI とタイトル → HTML の順に取り出す。どちらもコールバック方式
        (同期では取れない)なので、素直に入れ子にする。

        base に view.url() ではなく document.baseURI を使うのは、ページ自身が
        <base href> を持っている場合にその解決結果を尊重するため。"""
        try:
            if self._done:
                return
            self._view.page().runJavaScript(
                "[document.baseURI, document.title]", self._on_meta
            )
        except Exception as e:
            self._fail(f"ページを読み取れませんでした（{type(e).__name__}）")

    def _on_meta(self, result) -> None:
        try:
            if self._done:
                return
            base_url = self._url
            title = ""
            if isinstance(result, (list, tuple)) and result:
                if isinstance(result[0], str) and result[0]:
                    base_url = result[0]
                if len(result) > 1 and isinstance(result[1], str):
                    title = result[1]
            if not base_url:
                base_url = self._view.url().toString() or self._url
            self._view.page().toHtml(
                lambda html: self._on_html(html, base_url, title)
            )
        except Exception as e:
            self._fail(f"ページを読み取れませんでした（{type(e).__name__}）")

    def _on_html(self, html, base_url, title) -> None:
        try:
            if self._done:
                return
            if not html:
                self._fail("中身が空でした")
                return
            self._done = True
            self._dispose()
            self._on_ready(html, base_url, title)
        except Exception as e:
            # ここまで来たら _done は立っている。二重通知にはならない。
            print(f"[tray-tools] 取り込み後の処理に失敗: {e}", file=sys.stderr)

    # -- 後始末 ------------------------------------------------------
    def _fail(self, message: str) -> None:
        if self._done:
            return
        self._done = True
        self._dispose()
        try:
            self._on_failed(message)
        except Exception as e:
            print(f"[tray-tools] 取り込み失敗の通知に失敗: {e}", file=sys.stderr)

    def _dispose(self) -> None:
        """view を畳んで _ACTIVE から抜ける。_done を立てた後にだけ呼ぶこと。

        deleteLater にするのは、ここがだいたい view 自身のシグナル/コールバックの
        中だからで、その場で消すと足元が崩れる。"""
        self._timeout.stop()
        try:
            self._view.stop()
            self._view.deleteLater()
        except (RuntimeError, AttributeError):
            pass
        _ACTIVE.discard(self)


def capture(url: str, on_ready, on_failed, timeout_ms: int = None,
            settle_ms: int = None) -> None:
    """URL を1つ取り込む。結果はコールバックで返る(この関数はすぐ戻る)。"""
    job = SiteCapture(
        url,
        on_ready,
        on_failed,
        _int_setting(timeout_ms, DEFAULT_TIMEOUT_SECONDS * 1000),
        _int_setting(settle_ms, DEFAULT_SETTLE_MS),
    )
    job.start()


# ---------------------------------------------------------------
# 入口(トレイメニュー・ランチャからはここを呼ぶ)
# ---------------------------------------------------------------
def open_site(app_settings: dict, settings_path, presenter_path, notify,
              parent=None, url: str = None) -> None:
    """URL を尋ねて取り込み、発表者ツールとして既定のブラウザで開く。

    url を渡せば尋ねずにそのまま取り込む(IPC など、外から名指しで呼ぶとき用)。
    notify(タイトル, 本文) は通知に使う。この関数は例外を外へ出さない。"""
    try:
        target = normalize_url(url) if url else ask_url(parent, app_settings)
        if not target:
            if url:
                notify(DIALOG_TITLE, f"URL として読めません\n{url}")
            return

        presenter_path = Path(presenter_path)
        if not presenter_path.exists():
            notify(DIALOG_TITLE, f"発表者ツールが見つかりません\n{presenter_path}")
            return

        tools = app_settings.get("tools") or {}
        timeout_ms = _int_setting(
            tools.get("web_presenter_timeout_seconds"), DEFAULT_TIMEOUT_SECONDS
        ) * 1000
        settle_ms = _int_setting(tools.get("web_presenter_settle_ms"), DEFAULT_SETTLE_MS)
        strip_js = bool(tools.get("web_presenter_strip_scripts"))

        # 溜まった古い取り込みはここで片付ける(取り込みのついで)。
        sweep_stale_pages()

        notify(DIALOG_TITLE, f"読み込み中…\n{target}")

        def on_ready(html, base_url, title):
            try:
                page = build_page(presenter_path, html, base_url, title or target, strip_js)
                path = write_temp_page(page)
                os.startfile(str(path))
            except OSError as e:
                # os.startfile は .html に関連付けが無いと投げる。書き出しの失敗もここ。
                notify(DIALOG_TITLE, f"開けませんでした\n{e}")
                return
            except Exception as e:
                notify(DIALOG_TITLE, f"取り込みに失敗しました\n{type(e).__name__}: {e}")
                return
            remember_url(app_settings, settings_path, target)
            notify(DIALOG_TITLE, f"取り込みました\n{title or base_url}")

        def on_failed(message):
            notify(DIALOG_TITLE, f"{message}\n{target}")

        capture(target, on_ready, on_failed, timeout_ms, settle_ms)
    except Exception as e:
        # メニューのスロットから呼ばれる。ここで投げ切ると常駐ごと落ちる。
        notify(DIALOG_TITLE, f"取り込みを開始できませんでした\n{type(e).__name__}: {e}")
