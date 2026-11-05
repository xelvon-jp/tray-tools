---
id: 20261105-04-copilot-review-fixes
kind: implement
repo: xelvon-jp/tray-tools
base_branch: main
base_sha: <依頼の1行に書く>
branch: muse/20261105-04-copilot-review-fixes
result: handoff/20261105-04-copilot-review-fixes.result.md
hops: 0
author: Claude（陽太の依頼で作成）
---

# [Claude依頼] Copilot 状態監視の3コミット（ff0f81f / 10a1d11 / 92c9e27）のレビュー指摘を直す

## 目的
今日 main に入った Copilot 状態監視の変更を Claude がレビューした。テストは全部通っているが、
直すべき点が3つある。陽太さんから「Muse に直させて」と頼まれた。

## やること

### 1. 旧設定 `copilot_status.enabled` の引き継ぎが働いていない（いちばん大事）
- `copilot_watchdog.CopilotWatchdog.__init__` は「`bar` が無ければ `enabled` を初期値にする」と書いてあるが、
  受け取る `app_settings` は `settings.load_settings()` の結果で、`DEFAULT_SETTINGS` と深くマージ済み。
  `settings.py` の `DEFAULT_SETTINGS["copilot_status"]` に `"bar": True` があるので、**`bar` は必ず入っていて
  `elif "enabled"` の分岐には絶対に入らない**。結果、旧仕様で OFF にしていた PC でもバーが出る。
- 子プロセス側（`copilot_status_process.py` の `StatusWatcher.__init__` の `_section.get("bar", True)` と
  `_refresh_bar_setting`）も同じくマージ済みの設定を読むので、同じ問題がある。親と子で判定がずれる余地もある。
- 直し方：
  - 「バーを出すか」の判定を1つの関数にまとめ、**マージ前の settings.json の中身**（ファイルを直接読んだ
    `copilot_status` 節）で決める：`bar` があればそれ → 無く `enabled` があればそれ → どちらも無ければ `True`
    （どちらも無い＝この機能を初めて使う PC、の扱いは今の意図どおり True のまま）
  - 置き場所は `copilot_watchdog.py`（常駐からも子からも安全に import できる。UIA/comtypes を読まないこと）。
    親（`CopilotWatchdog`）と子（`StatusWatcher` の初期化と `_refresh_bar_setting`）の両方からそれを使う
  - `settings.py` の `DEFAULT_SETTINGS` から `copilot_status` の `bar` / `enabled` を外す（既定値で上書き
    されないように）。コメントは「既定値をここに置かない理由」に書き換える
  - `set_bar_enabled` / `_save_section` の「該当キーだけ差し替える」書き方はそのままでよい
- テスト（`tests/test_copilot_status.py`）に足す：
  - settings.json に `copilot_status` が無い → True
  - `{"enabled": false}` だけ → False（旧仕様で OFF の PC）
  - `{"enabled": false, "bar": true}` → True（`bar` が勝つ）
  - 子と親が同じ答えを返す（同じ関数を通ること）

### 2. `_activate_copilot` の ctypes に argtypes / restype が無い
- `feature_screen.ScreenFeature._activate_copilot` が `ctypes.windll.user32` の `IsWindow` / `IsIconic` /
  `ShowWindow` / `SetForegroundWindow` を型の指定なしで呼んでいる。リポジトリの決まり（AGENTS.md / CLAUDE.md
  「ctypes は argtypes / restype を必ず指定する」）に反していて、窓のハンドルの値が大きいと引数の変換で
  例外になり、押しても何も起きない（例外は受け止めているので落ちはしない）。
- 直し方：
  - `ctypes.windll.user32` の関数に型を書き込まない（他のモジュールも同じ関数オブジェクトを使うので、
    書き換えると影響が広がる）。**専用の `ctypes.WinDLL("user32", use_last_error=True)`** を作り、
    `HWND`（`wintypes.HWND`）引数・`BOOL` 戻り値（`ShowWindow` は `(HWND, int)`）を指定して使う
  - hwnd は JSON から来る int なので、`wintypes.HWND(hwnd)` で渡す
  - Linux（Muse の VM）では `ctypes.WinDLL` が無いので、作るのは呼ばれたとき（関数の中）にすること。
    import 時に作ると Linux でテストが import で落ちる
- テスト：Windows でしか確かめられない部分は「未実施・実機確認待ち」でよい。Linux で回せる範囲
  （import しても落ちない、など）だけ回す

### 3. `copilot_turn_status.json` が .gitignore に無い
- 子プロセスがリポジトリ直下に `copilot_turn_status.json`（と書きかけの `copilot_turn_status.json.tmp`）を
  書くようになったが、同じ種類の `copilot_status_state.json` / `copilot_loop_status.json` と違って除外されて
  いない。このリポジトリは public なので、`git add -A` で窓のハンドルなどが公開されうる。
- 直し方：`.gitignore` の `copilot_loop_status.json` の近くに、同じ書き方（1行コメント＋パス）で2つとも足す

### 触らないこと（Claude が実機で確かめる。直さず、所見があれば結果ファイルに書くだけ）
- 進捗パネルの Copilot 行を押したときの前面化（パネルはフォーカスを取らない窓なので、このPCの前面化ロックで
  `SetForegroundWindow` が黙って失敗する懸念がある。`traytools_send.py` の `FOREGROUND_COMMANDS` 付近の
  コメントに同じ現象の実測がある）
- `WAITING_AI_DECAY_SECONDS = 6`（業務PCの M365 Copilot は応答開始まで6秒以上かかることがある。秒数は
  業務PCの実測で決める）

## kind ごとの違い
この依頼の kind は上の frontmatter のとおり（implement）。

- Muse の環境（Linux VM）で回せるテスト（`QT_QPA_PLATFORM=offscreen` の `tests/test_copilot_status.py` など）は
  回し、結果を書く
- Windows 実機でしか確かめられない項目（常駐の再起動、UIA、user32 の呼び出し、実画面の目視）は、やったふりを
  せず結果ファイルに「**未実施・実機確認待ち**」と書いて項目を挙げる

## 守ること
- **`main` には何も push しない**。push するのはブランチ `muse/20261105-04-copilot-review-fixes` だけ
- **ブランチ `muse/20261105-04-copilot-review-fixes` は Claude が作成済み**（依頼書と同じコミットを指している）。
  新しく作らず、そこへ push する
- 触ってよいパス：
  - `handoff/20261105-04-copilot-review-fixes.result.md`（新規。必ず作る）
  - `copilot_watchdog.py`
  - `copilot_status_process.py`
  - `settings.py`
  - `feature_screen.py`
  - `.gitignore`
  - `tests/test_copilot_status.py`
  - `README.md`（`copilot_status.enabled` / `bar` の説明を直す必要があるときだけ）
- それ以外のファイルは触らない
- push は**1回・1コミット**にまとめる
- コミットメッセージは日本語。1行目に要約、本文に「なぜそうしたか」
- このリポジトリは public。秘密情報（トークン・パスワード・個人のファイルパスなど）を書かない
- 改行は LF（Python で書き戻すときは `newline=""`）

## 作業中に main が進んでいた場合
- 調べ物・判断は base_sha 時点の内容で行う
- ブランチは作成済みで、base_sha を指している。そのまま使う
- 変えるファイル自体が main 側で変わっていて衝突しそうなら、無理に合わせず status: blocked で止めて理由を書く

## 結果ファイルの書式
`handoff/20261105-04-copilot-review-fixes.result.md` を TEMPLATE.md の「結果ファイルの書式」の形で作ってください
（frontmatter: id / status / base_sha / head_sha / branch / approvals（内訳つき） / started / finished、
本文: 結果 / 未実施・実機確認待ち / base と現在の main の差 / 連携についてのメモ）。
