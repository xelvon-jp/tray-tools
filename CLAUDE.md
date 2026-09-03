# tray-tools 作業ルール

Windows 常駐トレイアプリ（Python 3.14 + PySide6）。トレイアイコンから画面キャプチャ・音声出力切替・画面ミラーなどをまとめて扱う。

詳しい仕様は `README.md`（このリポジトリのいちばん厚い文書）にある。**ここに書くのは、読まずに触ると壊すところだけ。**

## 改行コード

**全ファイル LF。** `.gitattributes` は無く `core.autocrlf=false`。

- 確認は `git ls-files --eol`（`i/lf w/lf` なら LF）と `git diff --stat`。
- **「既存は CRLF」という報告を信じないこと。** 実際に何度も誤報告があった。必ず上のコマンドで確かめる。
- **Python で書き戻すと CRLF になる。** `open(path, 'w')` は既定 `newline=None` で `\n` → `\r\n` に変換する。書き戻すときは `newline=''` を指定するか、`'rb'` / `'wb'` でバイト列のまま扱う。

## Feature の規約

**Feature = トレイアイコンを1つ所有するもの。**

- `feature_*.py` に「`self.tray_icon` を持つ」「`hotkeys()` を返す」クラスを書き、`main.py` の `FEATURE_CLASSES` に足す。
- **抽象基底クラスは意図的に作っていない。** 2つしかないものに継承を被せる利点が無い。
- **アイコンを持たない能力は、普通のモジュールにする**（`snippets.py` / `clipboard_format.py` / `screen_ruler.py` など）。開閉の管理は `feature_screen.py` 側が持つ。

**通知領域のアイコンは2つに固定する方針。** 新しいトレイアイコンを増やさないこと。状態表示が要るならタスクバーウィジェット側へ。

## 落ちる書き方（すべて実際に踏んだもの）

- **COM オブジェクトを関数の外に出さない。** 親を変数に受けずに子だけ返すと即座に解放され、`0xC0000005` でプロセス即死する。例外ではないので `error.log` にも何も残らず、しかも落ちたり落ちなかったりする。
- **`mss.MSS()` を都度作らない。** `capture_grab._sct()` の使い回しを必ず通す。毎フレーム作ると COM/GC 衝突で落ちる（2026-08-28 に8回）。
- **`finally` の中の例外は捕まえられない。** `finally` で Qt オブジェクトを触るなら必ず `try` で囲む。
- **PySide6 はスロット内で例外が投げ切られるとプロセスごと終了する。** 外部から叩かれる入口（`_handle_connection` など）は必ず `try` で受ける。通常起動は `pythonw.exe` なので標準エラーもどこにも出ず、理由が何も残らない。
- **`comtypes.COMError` は `OSError` のサブクラスではない。** まとめて捕まえたつもりで抜ける。
- **`ctypes` は `argtypes` / `restype` を必ず指定する。** 64bit でハンドルが int に切り詰められてアクセス違反になる（`GetClipboardData` で実際に踏んだ）。

## 外から叩ける口（`main.py` の `_build_command_handlers`）

増やすほど誤操作の被害が広がる。**足してよいのは「常駐しているこのプロセスでなければできないこと」だけ。**

アプリの起動・ファイル操作のたぐいは足さない。呼ぶ側が直接できるので、わざわざ常駐を経由させる利点が無く、口を通る危険だけが増える。

`traytools_send.py` と `hooks/claude_hook.py` は **PySide6 を import しない。** Qt の import だけで1秒近くかかり、ホットキーやフックの体感がそのぶん遅くなる。

## 単一起動の判定

**名前付きミューテックス（`CreateMutexW`）で決める。`QLocalServer` では決まらない。**

- **Windows の名前付きパイプは、同じ名前で複数のサーバインスタンスを作れる。** 実測で、別プロセスが待ち受けている名前に対して `listen()` が `True` を返した。**「立てられたから自分が先着」という判定が原理的に成り立たない**
- 「調べてから立てる」形は、検査と確保の間に隙間がある（同時に起動した2つが両方「誰も居ない」と判断しうる）
- `CreateMutexW` は作成と「既にあったか」の判定が1回のシステムコールで済むので隙間が無い。プロセスが死ねば OS が必ず手放すので、**クラッシュ後に起動できなくなる問題も起きない**

`QLocalServer`（`SINGLE_INSTANCE_KEY`）は**外部コマンドを受ける口としてだけ**使う。判定には使わない。

**再起動（`_restart`）では、後継を起こす前にミューテックスを手放すこと。** 手放さないと新しい方が「すでに起動しています」と判断して引き返す。

## プロセス一覧では常に2つに見える（二重起動ではない）

**`.venvs\tray-tools\Scripts\python.exe` / `pythonw.exe` は起動用のスタブで、本物のインタプリタを子プロセスとして起こす。** 親も生き続けるので、タスクマネージャや `Get-CimInstance Win32_Process` では**同じコマンドラインが必ず2行並ぶ**。

```
10908 親37244  pythonw.exe R:\claude\tray-tools\main.py
48844 親10908  pythonw.exe R:\claude\tray-tools\main.py     ← 10908 の子。同じ1インスタンス
```

**これを二重起動と読み違えないこと。** 実際に一度読み違えて、存在しない不具合を追いかけた。見分け方は次のとおり。

- **親子関係を見る**（`ParentProcessId`）。片方がもう片方の親なら1インスタンス
- 起動時刻の差が**十数ミリ秒**なら、まず間違いなくこれ
- `capture_process.py` でも `mcp_server.py` でも同じように2つ並ぶ。**MCP サーバーは自分で子を作らない**ので、そこで2つ見えたら原因はスタブだと分かる
- 決定的な確かめ方: `venv\Scripts\python.exe -c "import time; time.sleep(4)"` を1つ動かして数える。**2つ見えれば環境の性質**

本当に二重起動しているかは、ミューテックスが取れるかで確かめる。

```
python -c "import main; print(main._acquire_single_instance())"
```

常駐が正しく1つ動いていれば `False`（＝既に誰かが握っている）が返る。

## 起動・再起動

**エージェントがアプリを起動・再起動しない。** 動いている常駐を落とすと、開いている付箋やミラーが道連れになる。反映が要るときは、トレイの「🔄 再起動」を押してもらう。

検証は `QT_QPA_PLATFORM=offscreen` で。窓を実画面に出さず、`QWidget.grab()` で PNG に落として確かめられる。

- **offscreen で `setMimeData()` を使うと終了時にセグメンテーション違反（139）が出る。** 実機とは無関係（`README.md` に理由あり）。
- offscreen では日本語フォントが解決されず豆腐になることがある。レイアウトの確認には使えるが、字が出ないことを不具合と読み違えないこと。

## 疑似エージェントループ（`agent_loop.py` / `copilot_loop.py`）

Copilot アプリを相手に「プロンプト送信 → 応答受信 → コード実行 → 結果貼り戻し」を自動で回す仕組み。

- **UIA だけを使う。キー送信もマウス操作もしない。** フォーカスを奪わないので、他の作業と並行しても事故が起きない
- **既定は dry-run。** `--auto` を明示しない限り PowerShell を実行せず、コードをログに残して止まる。初めての題材はまずここで安全に確かめる
- **危険パターン検知**（`copilot_loop.risky_lines`）でヒットしたら自動実行しない。人の判断を待って止まる。Copilot に理由を返して終わる
- **3層のタイムアウト**（PowerShell 単発 / 応答待ち / ループ全体）で無限ループを防ぐ
- **キャンセルはファイル方式**（`.copilot_loop_cancel`）。実行スレッドが詰まっていても次の周の頭で拾って止まる

### 応答テキストの取り方に決着済みの罠

`copilot_loop.Copilot.document_text` の docstring に詳しいが要点を再掲。3案を実測して「**`FindAll` の並びのまま `Text` 要素の名前を繋ぐ**」だけが正解だった。

- Text を座標順に並べる → 構文強調でトークンが割れ、コードが原形を失う
- `TextPattern.DocumentRange` → 一部のトークン境界で改行が入る（`@{` が `@` と `{` に割れる、`-Encoding UTF8` が2行になる）
- `FindAll` の返り順（DOM 順）は保たれるので、**並べ替えないこと**が肝

### wait_until_idle は「busy を見てから idle 復帰」に依存しない

前実装は「busy を見た後に idle に戻ったら完了」で作っていたが、生成が短すぎたり poll の隙間で busy を見逃すと**永久に idle 待ちになった**（301秒タイムアウトの実例あり）。いまは「送信後 grace → idle が settle_seconds 連続で完了 / busy を見たらタイマーをリセット」。busy を見逃しても止まる。

### new_response は送信直前の全文長から切り出す

`rsplit("Copilot の発言", 1)[1]` は会話が長くなると誤爆する。応答内に見出しとして同じマーカーが混ざったり、古いターンの断片が最後尾になったりして、27文字などの短片が返る。**送信直前の `snapshot_length()` を渡して、それより後ろだけを扱う**。

### 業務PC（M365 Copilot）への移植

`copilot_loop.SELECTORS` を書き換えるだけで対応できる作り（窓の class / title、入力欄の AutomationId、送信/停止ボタンの日本語名など）。**書き換える候補は `tools/uia_probe.py` が出してくれる**。`--watch 20` で `メッセージの送信 → メッセージの割り込み → Copilot と会話する` の遷移を眺めて、正しいボタン名を突き止める。

## 設定ファイル

`settings.json` を丸ごと書き戻さない。**ファイルを読み直して該当キーだけ差し替える**（丸ごと書くと未設定の既定値まで焼き込まれ、ファイルの姿が変わる）。`snippets.push_recent` / `launcher.save_bookmark` / `clipboard_preview._save_size` が同じ作法。

## git

- コミットメッセージは**日本語**。1行目に要約、本文に「なぜそうしたか」。
- **このリポジトリは public。** 個人的な除外は `.gitignore` ではなく `.git/info/exclude` へ（`.gitignore` は共有されるので「何を隠したか」が公開される）。

### push

**通常の push は確認なしでよい。** 単一PC・単独操作で、GitHub Actions も他の書き手もいないため競合しない（`threads-auto-post` で git を禁止しているのは Actions と `index.lock` が競合するからで、ここにその事情は無い）。

**ただし public なので、push の直前に必ずこれを通すこと。** 一度出せば取り消しは事実上効かない（キャッシュ・フォーク・検索）。危ないのは push ではなく**コミットの時点**なので、push を止めても守りにはならない。だから止める代わりに、毎回見る。

```bash
git diff --stat origin/main..HEAD
git ls-files --error-unmatch settings.json hooks/hook.log action.log crash.log error.log
git diff origin/main..HEAD | grep -inE "token|api[_-]?key|password|secret|bearer|[a-f0-9]{32,}"
git log origin/main..HEAD --format="%an <%ae>" | sort -u
```

- `settings.json` と各種 `*.log` が**追跡対象になっていないこと**（`--error-unmatch` が全部失敗すれば正しい）
- 追加行に秘密情報らしき文字列が無いこと
- 著者が noreply で統一されていること

**引っかかったら push せずに報告する。**

### 確認が要るもの（自動でやらない）

- **履歴を書き換える操作** … `push --force` / `rebase` / `reset --hard`
- **public ↔ private の切り替え**
- **新しいリポジトリを公開すること**
