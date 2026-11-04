---
id: 20261105-02-approve-watch
kind: implement
repo: xelvon-jp/tray-tools
base_branch: main
base_sha: (依頼の1行に書いてある)
branch: muse/20261105-02-approve-watch
result: handoff/20261105-02-approve-watch.result.md
hops: 1
author: Claude（陽太の依頼で作成）
---

# [Claude依頼] muse_post.py に approve / watch / 既存チャットへの投稿を足す

## 目的
陽太さんの要望が2つある。①Muse の承認カードのタップが多いので、**内容を確かめたうえで Claude 側が押す**ようにしたい ②サイドチャットが増えすぎるので、**一連のやり取りは同じサイドチャットで続けたい**。
あわせて、待っている間の監視を Claude ではなくスクリプトのループに任せ、Claude のトークン消費を減らす（Claude は最後に結果だけ読む）。
前回の修正（20261105-01）はレビュー済みで main に入っている。この続き。

## 実機で測った事実（Windows・2026-11-05、UIA）
**承認カード**：Group(ControlType 50026) の Name が `GitHubの承認リクエスト`。その子孫の Text(50020) が「ラベル → 値」の順に並ぶ。実例（上から順）：
- `Action` → `Push files to GitHub`
- `Branch or ref` → `muse/20261105-01-muse-post-fixes`
- `Repository owner` → `xelvon-jp`
- `Repository` → `tray-tools`
- `Files · 1 · File path` → `muse_post.py`、`Files · 2 · File path` → `muse_post_logic.py`、…（ファイルの数だけ）
- `Files · 1 · Content` → 内容の先頭（長い）、…
- ほかに `Additional request details are not shown.`、`プレビューは省略されています`、説明文の Button(50000)（Muse の説明と「サイドチャット「…」から来ています」）
- 同じ Group の中に Button `許可する` と `許可しない`
- 同じラベルが DataItem(50007) としても重複して出る（Text の方だけ見ればよい）
- 承認待ちの間、窓の上部に Button `Muse、承認が必要です` が出る
- カードは**表示中のチャットの中**に出た。別のチャットを表示しているときにカードが見えるかは**未確認**

**チャット一覧**：一覧を開くと、各サイドチャットが Button で並ぶ。Name はチャット名、未読があると後ろに ` 未読のアップデート` や ` 未読のチャット` が付く（例：`NTT株価急落の買い判断 未読のアップデート`）。一覧が開いている間は `…チャットとサイドチャットを開く` ボタン（＝チャット名の取り出し元）は消え、`status` の `chat_title` は null になる。チャットの Button を Invoke するとそのチャットが表示される（実測：`Tray-tools レビューを実行` に切り替えられた）。

## やること
1. **`approve` サブコマンド**：`approve --owner X --repo Y --branch muse/<id> --allow-path P [--allow-path P ...] [--dry-run]`
   - 純関数 `parse_approval_card(items)`（items は (ControlType, Name) の並び）でカードを辞書にし、純関数 `approval_decision(card, expect)` で判定する
   - **許可してよい条件（すべて満たすとき だけ）**：カードがちょうど1枚／Action が `Push files to GitHub` に完全一致／Branch・owner・repo が完全一致／ファイルが1件以上あり、すべて `--allow-path` に含まれる／`--branch` が `muse/` で始まる（`main` などは引数の時点で拒否）
   - 条件外・読めない項目がある・未知のラベルがある場合は**押さない**（`許可しない` も押さない。人に残す）。理由を JSON で返し exit 1
   - 押したら、カードが消えたことを確かめて返す
2. **`watch` サブコマンド**：`watch --repo-dir PATH --owner X --repo Y --branch muse/<id> --path <result> --allow-path ... [--timeout 3600] [--interval 15]`
   - ループで ①結果ファイルがブランチに載ったら `{"ok":true,"status":"done",...}` で終わる ②承認カードがあれば approve と同じ判定で、条件に合えば押す（押した回数を数える）③条件に合わないカードがあれば押さずに `{"ok":false,"status":"needs_human","reason":...}` で終わる ④`Muse、承認が必要です` が出ているのにカードが見えない（別チャットにある等）なら `needs_human` で終わる ⑤タイムアウト
   - 承認を押したら `approved` の配列に（時刻・ファイル一覧）を残す
3. **`post --chat TITLE [--expect-text S]`**：一覧を開き、Name が TITLE と完全一致、または TITLE＋（` 未読のアップデート` か ` 未読のチャット`）のチャット Button を**ちょうど1つ**見つけて Invoke し、そのチャットに投稿する（`--current-chat` と同じ扱い）。`--expect-text` を付けたら、切り替え後の画面にその文字列を含む Text があることを確かめてから投稿する（違うチャットに誤投稿しないため。Claude は前回の依頼 id を渡す）。見つからない・2つ以上なら何もせず exit 1
4. 判定は純関数に分けて `muse_post_logic.py` に置き、`tests/test_muse_post.py` に Linux で回るテストを足す（上の実例をそのままフィクスチャにする。条件外のケース：main ブランチ・許可外のパス・Action 違い・カード2枚・ファイル0件・未知ラベル）
5. 冒頭の使い方の説明と `VERSION` を更新する

## 守ること
- 触ってよいパス：`muse_post.py`、`muse_post_logic.py`、`tests/test_muse_post.py`、`handoff/20261105-02-approve-watch.result.md`（新規）
- **キー送信・マウス操作・SetForegroundWindow・SetFocus は使わない**。`ctypes.cast` 禁止（AGENTS.md）
- **ブランチ `muse/20261105-02-approve-watch` は Claude が作成済み**。新しく作らず、そこへ **1回・1コミット**で push。`main` には push しない。改行は LF
- 迷ったら「押さない」側に倒す

## 結果ファイルの書式
前回と同じ（frontmatter に id / status / base_sha / branch / approvals / started / finished。本文は「## 変更内容」「## 回したテストと結果」「## 未実施・実機確認待ち」「## 試運転についてのメモ」）。
