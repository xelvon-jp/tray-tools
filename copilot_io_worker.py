# copilot_io_worker.py
# copilot_io の裏方。Copilot への UIA 操作を、呼び出し側のプロセスの
# 外で1回だけ行う。
#
# なぜ別プロセスなのか: UIA(comtypes) を常駐の中で動かすと、pycaw と
# 同居して GC の拍子に常駐ごと即死する(実測あり。copilot_status_process.py
# の冒頭)。だから UIA を使う側を常駐から追い出す。エディタの窓が
# トレイ(常駐)の中にあっても あっふ の中にあっても、経路はこの裏方に
# 1本化する(copilot_io.SubprocessTransport が呼ぶ)。
#
# 使い方:
#   標準入力に JSON を1つ渡す: {"op": "send", "text": "..."}
#   標準出力に JSON を1行返す: {"ok": true, ...} / {"ok": false, "error": "…"}
#   op は snapshot / input / send / fill / attach_test / probe /
#   scan / scan_buttons / press_send / latest_download /
#   press_download / send_escape / profiles / wait / response /
#   blocks。
#   依頼に "profile" (辞書か組み込みの名前) が
#   あれば、その投げ先で Copilot を作る (無ければ従来どおり自動選択)。
#   診断は標準エラーへ。標準出力には応答の JSON だけを出す。
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _run(request):
    import copilot_io

    op = request.get("op")
    if op == "profiles":
        # プロファイルの並びは transport を作らずに返す (窓が無くても
        # 一覧だけは返せる。設定画面の「組み込み」表示用)。
        import copilot_loop

        return {"ok": True, "profiles": copilot_loop.profiles()}
    transport = copilot_io.CopilotLoopTransport(
        profile=request.get("profile"))
    try:
        if op == "snapshot":
            return {"ok": True, "length": transport.snapshot_length()}
        if op == "input":
            return {"ok": True, "input_text": transport.input_text()}
        if op == "send":
            # attachments (1.58) は、本文を入れる前に copilot 側で
            # 添付するファイルのフルパス。結果 (できた・できな
            # かったファイル名) も応答に含めて親へ返す。添付が
            # 無いときは従来どおりの呼び方にする (添付を知らない
            # transport の差し替え口を壊さないため)。
            attachments = request.get("attachments")
            if attachments:
                sent = bool(transport.send_prompt(
                    request["text"], attachments=attachments))
            else:
                sent = bool(transport.send_prompt(request["text"]))
            return {"ok": True, "sent": sent,
                    "attach_done": list(
                        getattr(transport, "attach_done", []) or []),
                    "attach_failed": list(
                        getattr(transport, "attach_failed", []) or [])}
        if op == "fill":
            attachments = request.get("attachments")
            if attachments:
                filled = bool(transport.fill_prompt(
                    request["text"], attachments=attachments))
            else:
                filled = bool(transport.fill_prompt(request["text"]))
            return {"ok": True, "filled": filled,
                    "attach_done": list(
                        getattr(transport, "attach_done", []) or []),
                    "attach_failed": list(
                        getattr(transport, "attach_failed", []) or [])}
        if op == "attach_test":
            # 添付テスト (1.61): 添付してチップを外す。送信はしない。
            attach_ok, message = transport.attach_test(request["path"])
            return {"ok": True, "attach_ok": bool(attach_ok),
                    "message": str(message)}
        if op == "probe":
            return {"ok": True, "candidates": transport.probe_input()}
        if op == "scan":
            return {"ok": True, "candidates": transport.scan_input()}
        if op == "scan_buttons":
            return {"ok": True, "candidates": transport.scan_buttons()}
        if op == "press_send":
            return {"ok": True, "pressed": bool(transport.press_send())}
        if op == "latest_download":
            # 診断の目印 (1-43) とリセットの目印 (1-52)、候補の
            # 全件 (1-57) も同じ走査で取る。scan を持たない
            # transport (テストの fake 等) では候補だけ返す。
            scanner = getattr(transport, "latest_scan", None)
            if scanner is not None:
                scan = scanner() or {}
                return {"ok": True, "candidate": scan.get("candidate"),
                        "candidates": scan.get("candidates"),
                        "marker": scan.get("marker"),
                        "reset_marker": scan.get("reset_marker")}
            return {"ok": True, "candidate": transport.latest_download()}
        if op == "press_download":
            # key は窓側が選んだ候補 (名前+クラス名、1-57)。
            # 無ければ裏方は従来どおり最新の1件を選ぶ。
            return {"ok": True,
                    "pressed": bool(
                        transport.press_download(request.get("key")))}
        if op == "send_escape":
            return {"ok": True, "status": transport.send_escape()}
        if op == "wait":
            done, elapsed = transport.wait_until_idle(float(request["timeout"]))
            return {"ok": True, "done": bool(done), "elapsed": float(elapsed)}
        if op == "response":
            text = transport.new_response(
                request.get("previous_length") or 0,
                sent_prompt=request.get("sent_prompt"))
            return {"ok": True, "text": text or ""}
        if op == "blocks":
            return {"ok": True, "blocks": list(transport.code_blocks() or [])}
        return {"ok": False, "error": f"知らない操作です: {op}"}
    finally:
        transport.close()


def main() -> int:
    # 入力は UTF-8 で読む(親が UTF-8 で書く。標準入力の既定の文字コードは
    # 環境で変わるので、バイト列から直接読む)。
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        request = json.loads(raw or "{}")
    except ValueError as e:
        result = {"ok": False, "error": f"依頼の JSON が読めませんでした（{e}）"}
    else:
        try:
            result = _run(request)
        except Exception as e:  # noqa: BLE001  理由は親が利用者への文にする
            result = {"ok": False, "error": str(e) or type(e).__name__}
    # 出力も UTF-8 で書く(コンソールの文字コードで化けないように)。
    sys.stdout.buffer.write(
        json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
