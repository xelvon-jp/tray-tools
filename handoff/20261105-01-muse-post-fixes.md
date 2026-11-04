---
id: 20261105-01-muse-post-fixes
kind: implement
repo: xelvon-jp/tray-tools
base_branch: main
base_sha: (依頼の1行に書いてある)
branch: muse/20261105-01-muse-post-fixes
result: handoff/20261105-01-muse-post-fixes.result.md
hops: 1              # 20261104-03 のレビューの続き
author: Claude（陽太の依頼で作成）
---

# [Claude依頼] muse_post.py のレビュー指摘を直す（20261104-03 の続き）

## 目的
前回あなたが出したレビュー（ブランチ `muse/20261104-03-muse-post-review` の結果ファイル）の指摘を実装する。Claude 側でレビュー内容は妥当と判断した。

## やること
指摘の番号は前回の結果ファイルのもの。
1. **#11 を最初に**：判断ロジックの純関数（`is_blank` 〜 `redact`、`now_iso`、`MUSE_PROFILE` と時間の定数）を新しいモジュール `muse_post_logic.py` に移す。こちらは comtypes・copilot_loop を import しない。`muse_post.py` はそこから import して使う（外から見た CLI の挙動は変えない）。`tests/test_muse_post.py` の純関数テストは `muse_post_logic` を import するように変え、**Linux VM で実際に回して通ることを確かめる**
2. #3：`check_before_invoke` にも busy の確認を足す（テストも）
3. #1：新しいチャットの ready 判定に「Invoke 前と画面が変わったこと」を足す。純関数側は「前の状態と今の状態を受け取って判定する」形にする（例：チャット名・子孫数・メッセージボタン数の組が前と同じなら未切り替え）。Invoke 直後の1回目の scan は捨てる、でもよい
4. #2：`set_value` の直前と送信 Invoke の直前に、同じ要素を `read_value` で読み直して確認する
5. #5：SetValue 後・送信前に例外が起きたら `_restore_input` を試み、`steps` に残す。送信 Invoke の後の例外は「送れたか不明」と区別する
6. #6：新規チャット作成の途中で止まったとき、`reason` に「一覧を開いたまま」「空のサイドチャットが残っている」を明示する
7. #7：`--path` 無しで成功したときは `ok: true` ではなく `{"ok": false, "appeared": true, "reason": "ブランチは現れたが完了は未確認(--path を付けてください)"}` で exit 1。冒頭の使用例を `--path` 付きに直す
8. #8・#9：各 git 呼び出しの timeout を「残り時間と GIT_TIMEOUT の小さい方」にする。成功時の `sha` は fetch 後の `rev-parse refs/remotes/origin/<branch>` で取り、それで `cat-file` も確かめる
9. #12・#13 のうち、Linux でも回せる範囲のテストを足す（`cmd_post` を偽の Muse で進行確認できるよう差し替え口を作る、`cmd_wait` を一時リポジトリで確かめる、など）。Windows でしか確かめられないものは結果ファイルに「未実施・実機確認待ち」と書く
- #4・#10（確信度 低）は今回は直さない

## 守ること
- 触ってよいパス：`muse_post.py`、`muse_post_logic.py`（新規）、`tests/test_muse_post.py`、`handoff/20261105-01-muse-post-fixes.result.md`（新規）。それ以外は触らない
- **キー送信・マウス操作・SetForegroundWindow・SetFocus は使わない**。`ctypes.cast` 禁止（AGENTS.md の COM ルール）
- **ブランチ `muse/20261105-01-muse-post-fixes` は Claude が作成済み**。新しく作らず、そこへ **1回・1コミット**で push する。`main` には push しない。改行は LF
- 調べ物・判断は base_sha 時点の内容で行う

## 結果ファイルの書式
`handoff/20261105-01-muse-post-fixes.result.md`：frontmatter に id / status(done|failed|blocked) / base_sha / branch / approvals（承認カードが出た書き込み操作の回数と内訳。読み取りは数えない）/ started / finished。本文は「## 変更内容（指摘番号ごと）」「## 回したテストと結果」「## 未実施・実機確認待ち」「## 試運転についてのメモ」。
