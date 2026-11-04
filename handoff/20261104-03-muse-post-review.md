---
id: 20261104-03-muse-post-review
kind: ask
repo: xelvon-jp/tray-tools
base_branch: main
base_sha: (依頼の1行に書いてある)
branch: muse/20261104-03-muse-post-review
result: handoff/20261104-03-muse-post-review.result.md
hops: 0
author: Claude（陽太の依頼で作成）
---

# [Claude依頼] muse_post.py を読んでレビューする（読み取りのみ）

## 目的
Claude→Muse 連携の投稿道具 `muse_post.py` の第三者レビュー。あわせて「ブランチを Claude が先に作っておけば、承認カードが push の1回だけで済むか」を確かめる試運転でもある。

## やること
1. `muse_post.py` と `tests/test_muse_post.py` を読む（AGENTS.md のルールも前提に）
2. 次の観点で問題を挙げる：安全確認の抜け（入力中の下書きを上書きしうる経路・誤った要素を押しうる経路）、例外時に UI を中途半端な状態で残す経路、`wait` の判定の誤り、テストの抜け
3. 各指摘に「どの関数・何行目あたり・どういう入力で何が起きるか・直し方の案」を書く。確信度（高/中/低）も添える。問題が無ければ無いと書いてよい

## 守ること
- 読み取りだけ。コードは変えない。変更してよいのは結果ファイルの新規作成だけ
- **ブランチ `muse/20261104-03-muse-post-review` は Claude が作成済み**（base_sha を指している）。新しく作らず、そこへ **1回・1コミット**で push する
- `main` には何も push しない。改行は LF

## 結果ファイルの書式
`handoff/20261104-03-muse-post-review.result.md`：frontmatter に id / status(done|failed|blocked) / base_sha / branch / approvals（承認カードが出た書き込み操作の回数と内訳。読み取りは数えない）/ started / finished。本文は「## 指摘」「## 試運転についてのメモ」。
