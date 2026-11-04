---
id: 20261105-02-approve-watch
status: done
base_sha: b36a896ba7ac09cbabb19c6af685d5d9323993bd
branch: muse/20261105-02-approve-watch
approvals: 1（push_files 1回のみ。ブランチは Claude が作成済みで、こちらでは作っていない。読み取りは承認なし）
started: 2026-11-05T02:13:00+09:00
finished: 2026-11-05T02:17:00+09:00
---

# muse_post.py に approve / watch / post --chat を足した結果

対象は base `b36a896`（前回の修正が main に入った状態）。変更は許可された4パスだけ。`main` には push していない。改行は LF。

## 変更内容

- **純関数（`muse_post_logic.py`）**
  - `split_approval_cards(items)`：窓の子孫の並びから、Group「GitHubの承認リクエスト」ごとの部分並びを切り出す。
  - `parse_approval_card(items)`：カード1枚を辞書化する。Text の「ラベル → 値」の並びを読み、DataItem の重複は無視する。知っているラベルは Action / Branch or ref / Repository owner / Repository / `Files · N · File path|Content` のみで、それ以外・余った文字列は `unknown` に集める。ファイルは番号順に並べ、抜けがあれば `files_complete: false`。『許可する』『許可しない』の個数も数える。単独で出る注記（`Additional request details are not shown.` など2件）は既知として扱う。
  - `approval_decision(card, expect)`：押してよい条件（カードの内容で判定できる範囲：Action が `Push files to GitHub` に完全一致、Branch・owner・repo が完全一致、ファイルが1件以上で番号が揃い、すべて `--allow-path` に含まれる、未知項目なし、重複なし、許可／拒否ボタンが各1個、`--branch` が `muse/` で始まる）をすべて満たすときだけ None を返す。カードが「ちょうど1枚」であることは呼び出し側（`_check_approval`）で見ている。
  - `match_chat_buttons(button_names, title)`：完全一致、または末尾が「 未読のアップデート」「 未読のチャット」のものを集める。
  - `summarize` に `approval_cards`（Group の数）と `approval_banner`（『Muse、承認が必要です』の有無）を足した（status の出力にも追加）。
- **`approve` サブコマンド**：`--owner/--repo/--branch/--allow-path`（複数可、必須）と `--dry-run`。branch が `muse/` で始まらない・allow-path が空なら引数エラー（exit 2）。カードが無い／条件に合わない場合は何も押さず（『許可しない』も押さない）理由を JSON で返して exit 1。押した場合はカードが消えたことを poll で確かめてから `approved: true` を返す。カードはあるはずなのに見えず、代わりに『承認が必要です』が出ている場合は、その旨を理由に返す。
- **`watch` サブコマンド**：`--repo-dir/--owner/--repo/--branch/--path/--allow-path` と `--timeout`（既定3600）／`--interval`（既定15）。ループで ①結果ファイルがブランチに載れば `{"ok": true, "status": "done", "sha": ...}`、②承認カードがあれば approve と同じ判定で押し、`approved` 配列に時刻とファイル一覧を残す、③条件に合わないカード、または『承認が必要です』が出ているのにカードが見えない場合は押さずに `status: "needs_human"` で終了、④時間切れは `status: "timeout"`。Muse アプリが見えない環境では承認の監視だけを省き、結果の到着監視は続ける（その旨を `note` に出す）。
- **`post --chat TITLE [--expect-text S]`**：切り替え前に表示中のチャットの下書き確認（`check_initial`）を通し、一覧を開いて名前に合う Button がちょうど1つだけあるものを Invoke する。切り替え後は表示チャット名が TITLE と一致し、かつ `--expect-text` があれば画面の Text にその文字列が含まれることを確かめてから、表示中チャットへの投稿と同じ流れで送る。見つからない・複数ある・切り替わらない・expect が無い場合は投稿せず exit 1。`--current-chat` との同時指定と、`--expect-text` の単独指定は引数エラー（exit 2）。
- 冒頭の使い方に approve / watch / post --chat を追加し、VERSION を `20261105-02` に更新。`Muse.scan` は全 Button の (Name, 要素)・全 Text の Name・子孫の並びも返すようになった（承認解析とチャット選択のため）。

## 回したテストと結果

- `tests/test_muse_post.py` を **Linux VM の python3 で実行し、46/46 passed**（前回33件＋新規13件）。
  - 新規：カード解析（実例フィクスチャ、DataItem 無視、窓全体からの切り出し）、判定の許可・拒否ケース（許可外パス、Action 違い、ファイル0件、未知ラベル、ブランチ不一致、main ブランチ）、`cmd_approve` の偽 Muse テスト（押す／dry-run／拒否では両ボタンとも押さない／2枚／main は引数エラー／カード無し）、`cmd_watch` の一時リポジトリ＋偽 Muse テスト（done、拒否カードで needs_human、バナーだけでも needs_human、承認→結果到着で done になり approved に記録が残る）、`match_chat_buttons`、`post --chat` の偽 Muse テスト（切り替えて投稿、expect 無しで中止、見つからず中止）。
- 追加確認：`--version` が `20261105-02`、`approve --branch main` が exit 2 で即拒否、`post --chat × --current-chat` が exit 2。CR なし（LF）。新規・変更コードに `ctypes.cast`・キー送信・マウス操作・SetForegroundWindow・SetFocus は無い。

## 未実施・実機確認待ち

- **UIA の実操作は未実施**（Linux VM のため）。実機での確認待ち：実際の承認カードに対する `approve --dry-run`（解析が実例どおりに読めるか）→ 条件の合うカードでの実押しと消滅確認、`watch` の実運用（作業中の Muse の承認を押して done まで）、`post --chat` で実在のサイドチャットへ切り替えての投稿。
- 実測メモへの依存：カードの Text の並び順（ラベル→値）と、ファイル Content の Text が値として読める前提で解析している。実カードで値そのものがラベル文字列と同文になる等の並びがあれば `unknown` 側に倒れて押さない（安全側）。また一覧表示中は `chat_title` が null になる前提で、切り替え確認は Invoke 後の `chat_title` 一致で見ている。

## 試運転についてのメモ

- ブランチ `muse/20261105-02-approve-watch` は Claude が作成済みで、その先端が base_sha `b36a896ba7ac09cbabb19c6af685d5d9323993bd` と一致することを push 前に読み取りで確認した。こちらではブランチを作っていない。書き込みの承認カードは push の1回だけ（皮肉にも、この push 自体が将来 approve の対象になる形）。
- push 前チェックは AGENTS.md の代替手順で実施：変更は許可4パスのみ、秘密情報らしき文字列なし、追跡対象外ファイルの混入なし。検証は push 後にローカル clone の fetch と blob SHA 照合で行う。
