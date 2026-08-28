# pushover.py
# スマホへプッシュ通知を投げる窓口(https://pushover.net/)。トレイアイコンは持たない部品。
#
# トーストは目の前の画面にしか出ない。長い処理を回して席を外しているときに終わりを知る
# 手段が要る、というのがこれを足した理由。
#
# API は POST が1本あるだけ:
#   POST https://api.pushover.net/1/messages.json
#     token=<アプリのトークン> user=<ユーザーキー> message=<本文>
#     (任意: title / priority / sound)
# urllib で足りるので依存は増やさない。
#
# 【トークンの置き場所】
# token と user は settings.json へ書かない。あれは手で編集する前提の平文ファイルで、
# バックアップにも差分にもそのまま乗る。Windows の資格情報マネージャへ預ける
# (credential_store.py)。ログにも通知にも値そのものを出さないこと——このファイルの中で
# token/user を文字列に埋め込んでいるのは POST の本文を組み立てる1か所だけである。
#
# 【呼ぶ側への注意】
# send() は数秒かかりうる。Qt のメインスレッドから直接呼ぶと常駐アプリ全体が固まる。
# 必ず別スレッドで呼び、結果は呼び出し側でメインスレッドへ戻すこと(main.py の _Reply が
# シグナル経由で戻している)。ここは Qt に一切触らないので、別スレッドから呼んで安全。
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import action_log
import credential_store

API_URL = "https://api.pushover.net/1/messages.json"

# 資格情報マネージャ上の名前。「汎用資格情報」に この名前で並ぶ。
# UserName にユーザーキー、秘密のブロブにアプリのトークンを入れる。
CREDENTIAL_TARGET = "traytools:pushover"

# 応答を待つ上限(秒)。ここで待ちすぎると、呼んだ側(IPCの応答)も同じだけ待たされる。
TIMEOUT_SECONDS = 10

# 本文の上限(Pushover の仕様は1024文字)。切り詰めてから送る。
MAX_MESSAGE = 1024
MAX_TITLE = 250

# priority=2(緊急)は retry/expire の指定が要るうえ、受け取る側が確認するまで鳴り続ける。
# 外から叩ける口で出せる強さではないので、ここで上限を1(高)にする。
MIN_PRIORITY = -2
MAX_PRIORITY = 1


def store(user_key: str, app_token: str) -> bool:
    """ユーザーキーとアプリのトークンを資格情報マネージャへ預ける。"""
    user_key = (user_key or "").strip()
    app_token = (app_token or "").strip()
    if not user_key or not app_token:
        return False
    ok = credential_store.write(CREDENTIAL_TARGET, user_key, app_token)
    # 値は書かない。登録したという事実だけ残す。
    action_log.record("Pushover トークン登録", "成功" if ok else "失敗", "menu")
    return ok


def clear() -> bool:
    """登録を消す。もともと無ければ False。"""
    ok = credential_store.delete(CREDENTIAL_TARGET)
    action_log.record("Pushover トークン削除", "成功" if ok else "登録なし", "menu")
    return ok


def is_registered() -> bool:
    return credential_store.exists(CREDENTIAL_TARGET)


def _credential():
    """(user_key, app_token) を返す。未登録・片方欠けなら None。"""
    found = credential_store.read(CREDENTIAL_TARGET)
    if not found:
        return None
    user_key, app_token = found
    if not user_key or not app_token:
        return None
    return user_key, app_token


def _clean_priority(priority):
    if priority is None:
        return None
    try:
        return max(MIN_PRIORITY, min(MAX_PRIORITY, int(priority)))
    except (TypeError, ValueError):
        return None


def _describe_failure(exc) -> str:
    """失敗の理由を1行にする。トークンを含みうる文字列は決して混ぜないこと。

    Pushover は入力の誤りを 4xx と errors 配列で返す(例: application token is invalid)。
    本文にトークンは載らないので、そのまま見せてよい。"""
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
            errors = body.get("errors")
            if isinstance(errors, list) and errors:
                detail = " / ".join(str(e) for e in errors)
        except (ValueError, OSError):
            pass
        return f"HTTP {exc.code}" + (f": {detail}" if detail else "")
    if isinstance(exc, urllib.error.URLError):
        return f"接続できません: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def send(message: str, title: str = None, priority=None, sound: str = None):
    """1通送る。戻り値は (成功したか, 1行の説明)。

    例外は投げない。時間がかかる(数秒)ので、必ず別スレッドから呼ぶこと。"""
    message = (message or "").strip()
    if not message:
        return False, "本文が空です"

    credential = _credential()
    if credential is None:
        # 未登録のまま送りにいくと、無効なトークンで叩いて 4xx をもらうだけ。
        # 通信する前に断る。
        return False, "Pushover のトークンが登録されていません（トレイメニューの「スマホ通知」から登録してください）"
    user_key, app_token = credential

    fields = {
        "token": app_token,
        "user": user_key,
        "message": message[:MAX_MESSAGE],
    }
    if title:
        fields["title"] = str(title)[:MAX_TITLE]
    cleaned = _clean_priority(priority)
    if cleaned is not None:
        fields["priority"] = str(cleaned)
    if sound:
        fields["sound"] = str(sound)[:40]

    data = urllib.parse.urlencode(fields).encode("utf-8")
    # 本文の長さだけ記録する。中身は個人の作業内容になりうるので載せない。
    # ここはワーカースレッドから呼ばれるが、action_log はファイルへ1行足すだけで
    # Qt に触らないため安全(Qtのウィジェットには決してここから触らないこと)。
    try:
        request = urllib.request.Request(
            API_URL, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
        status = json.loads(body).get("status") if body else None
        if status == 1:
            action_log.record("Pushover 送信", f"{len(message)}文字", "external")
            return True, "Pushover へ送信しました"
        action_log.record("Pushover 送信 失敗", "status!=1", "external")
        return False, "Pushover が受け付けませんでした"
    except (urllib.error.URLError, OSError, ValueError) as e:
        detail = _describe_failure(e)
        action_log.record("Pushover 送信 失敗", detail, "external")
        return False, detail
    except Exception as e:  # 想定外でも呼び出し元(常駐アプリ)を巻き込まない
        print(f"[tray-tools] Pushover 送信で予期しない失敗: {type(e).__name__}", file=sys.stderr)
        return False, f"予期しない失敗: {type(e).__name__}"
