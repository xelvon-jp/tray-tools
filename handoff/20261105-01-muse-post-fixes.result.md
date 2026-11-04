---
id: 20261105-01-muse-post-fixes
status: done
base_sha: b49f6758a3c3762100c81235227aba9a2927e036
branch: muse/20261105-01-muse-post-fixes
approvals: 1（push_files 1回のみ。ブランチは Claude が作成済みで、こちらでは作っていない。読み取りは承認なし）
started: 2026-11-05T02:04:00+09:00
finished: 2026-11-05T02:09:00+09:00
---

# muse_post.py レビュー指摘の修正結果

対象は base `b49f675`。変更は許可された4パスだけ（`muse_post.py`、`muse_post_logic.py` 新規、`tests/test_muse_post.py`、この結果ファイル）。`main` には push していない。改行は LF。

## 変更内容（指摘番号ごと）

- **#11（最初に実施）**：判断ロジックの純関数（`is_blank`〜`redact`、`now_iso`）と `MUSE_PROFILE`・時間の定数を、新モジュール `muse_post_logic.py` へ移した。そちらは comtypes・copilot_loop を import しない（テストで AST を見て見張っている）。`_input_reason` が使う子孫数の基準（30）は copilot_loop と同じ値を logic 側にも持ち、両方直す旨をコメントに残した。`muse_post.py` は logic から import して使う。あわせて `muse_post.py` 側の UIA import を try/except で囲み、読めない環境でも import と `wait` は使えるようにした（窓を扱う生成時は「UIA 依存を読み込めない」と理由を返して止まる）。CLI の挙動・出力形式は変えていない。VERSION は `20261105-01` に更新。
- **#3**：`check_before_invoke` の先頭に busy（『停止』）確認を足した。他の確認関数と揃えた。テスト `test_before_invoke_busy` を追加。
- **#1**：純関数に `view_signature`（チャット名・子孫数・メッセージボタン数の組）を新設し、`check_new_chat_ready(s, before_sig)` が「Invoke 前と同じ画面の間は未切り替え」と判定するようにした。`cmd_post` は「新しいサイドチャット」を押す直前の signature を渡して待つ。表示中のチャット自体が空の作りたてでも、切り替わり前に通らなくなった。なお signature が偶然すべて一致する場合は「未切り替え」側に倒れて時間切れで止まる（誤投稿より安全な側）。テスト `test_new_chat_ready_needs_transition` と、偽 Muse での新規チャット経路テストを追加。
- **#2**：SetValue の直前に同じ要素を `read_value` で読み直し、空でなければ（読めない場合も含む）中止する。送信 Invoke の直前にも同じ要素を読み直し、送る文面と一致しなければ中止して `_restore_input` を試みるようにした。
- **#5**：`cmd_post` が「書いた後か」「送信 Invoke に入った後か」を追うようにし、書いた後・送信前の例外では一般の `except` でも `_restore_input` を試みて結果を `steps` に残す（戻せなかった場合は理由にもその旨を足す）。送信 Invoke 自体の例外と、Invoke 後の例外は「送れたか不明。二重投稿に注意」と区別して返す。テスト `test_cmd_post_restores_on_exception_after_write` を追加。
- **#6**：一覧を開いた後・空のサイドチャットを作った後の中止では、理由の末尾に「（開いた一覧が残っている）」「（作った空のサイドチャットが残っている）」を明示するようにした（一般の例外経路でも付く）。テスト `test_cmd_post_new_chat_leftover_is_reported` を追加。
- **#7**：`wait` で `--path` 無しにブランチが現れた場合は成功にせず、`{"ok": false, "appeared": true, "reason": "ブランチは現れたが完了は未確認(--path を付けてください)", "sha": ..., "branch": ...}` を exit 1 で返すようにした。冒頭の使用例も `--path handoff\xxx.result.md` 付きに直した。テスト `test_wait_without_path_reports_appeared_only` を追加。
- **#8**：各 git 呼び出しの timeout を `_git_timeout` で「全体の残り時間と GIT_TIMEOUT の小さい方」にした（残りが尽きたら TimeoutExpired として従来の timeout 返却に寄せる。`--timeout 0` の最初の1回だけは従来どおり GIT_TIMEOUT で試す）。テスト `test_git_timeout_budget` を追加。
- **#9**：成功時に返す `sha` は、fetch 後に `git rev-parse refs/remotes/origin/<branch>` で取り直した先端に変え、`cat-file` の確認もその sha に対して行うようにした。テストで「返る sha がブランチ先端と一致する」ことを確認している。
- **#12・#13（Linux で回せる範囲）**：`cmd_post` に差し替え口 `muse_factory` を作り、偽の Muse で「表示中チャットへの投稿成功」「dry-run は何も触らない」「下書きがあれば中止」「書いた後の例外で復旧」「新規チャットの残存明示」を確かめるテストを追加。`cmd_wait` は一時リポジトリ（src を作って clone）で「--path 付きで成功」「ファイルが無ければ timeout」「ブランチが無ければ timeout」を確かめるテストを追加。`read_message` のファイル読みと `_restore_input` の単体テストも追加。
- **#4・#10**：依頼どおり今回は直していない。

## 回したテストと結果

- `tests/test_muse_post.py` を **Linux VM の python3 で実行し、33/33 passed**（旧版は19件程度の純関数テストのみで、しかも import の時点で comtypes が無く Linux では起動しなかったもの）。
  - 内訳：従来の純関数テスト（logic へ import 先を変更）＋新規12件（遷移判定、busy、git 予算、一時リポジトリでの wait 3件、偽 Muse での cmd_post 5件、_restore_input、read_message、logic の無依存チェック）。
- 追加の確認：`python3 muse_post.py --version` が `20261105-01` を返すこと、`status` が Linux では「UIA 依存を読み込めない: No module named 'comtypes'」を理由に JSON で返ること（終了コード1）。両ファイルとも `py_compile` 通過。CR を含まないこと（LF）も確認済み。
- AGENTS.md の COM ルール：新規・変更コードに `ctypes.cast` は無い（grep で確認）。キー送信・マウス操作・SetForegroundWindow・SetFocus は使っていない（禁止の明記コメントのみ）。

## 未実施・実機確認待ち

- **UIA の実操作は未実施**：この VM は Linux で Muse アプリが無いため、`status`／`post` の実機動作、新規チャットの切り替わり判定（signature が実画面でどう変わるか）、再読確認の実挙動は確かめていない。実機（Windows）で `tests/test_muse_post.py` を venv の python で回すことと、`muse_post.py status` → `post --dry-run` → 実投稿の順の確認が残っている。
- signature の実測値：実画面では子孫数が投稿前後で同数になる可能性もあり、その場合は新規チャット投稿が時間切れで止まる側になる。実機で止まるようなら、signature の要素（例：投稿欄の要素の同一性）を足す必要がある。

## 試運転についてのメモ

- ブランチ `muse/20261105-01-muse-post-fixes` は Claude が作成済みで、その先端が base_sha `b49f6758a3c3762100c81235227aba9a2927e036` と一致することを push 前に読み取りで確認した。こちらではブランチを作っていない。書き込みの承認カードは push の1回だけ（hops: 1 の続き作業でも同型で回った）。
- push 前チェックは AGENTS.md の代替手順で実施：変更ファイルは上記の許可4パスのみ、秘密情報らしき文字列なし（新規コードに資格情報は含まれていない）、追跡対象外ファイルの混入なし。検証は push 後にローカル clone の fetch と blob SHA 照合で行う。
