---
id: 20261104-01-rules-survey
status: done
base_sha: 23638fcf193e8986b23ab3adb1f2764d44240194
branch: muse/20261104-01-rules-survey
approvals: 2（書き込み系のみ。内訳は試運転メモ参照）
started: 2026-11-04 23:14 JST
finished: 2026-11-04 23:16 JST
---

## 結果

読んだ対象は、base_sha 時点の `CLAUDE.md`（作業ルール本体）と `README.md`（仕様が本体だが、セットアップ・テスト・機能追加・MCP の節にルールが散在）。

総括：`CLAUDE.md` の中身の大部分は Claude Code 固有ではなく、このリポジトリ（とコード）を触る者すべてに当てはまるプロジェクトのルールだった。`AGENTS.md` への移行は十分可能で、Claude 固有として残るのは B の数件だけになる見込み。

### A. Muse にも当てはまる作業ルール（`AGENTS.md` に移すべきもの）

- 改行コード：全ファイル LF。`.gitattributes` は無く `core.autocrlf=false`。「既存は CRLF」という報告を信じず、`git ls-files --eol` と `git diff --stat` で確かめる
- Feature の規約：Feature = トレイアイコンを1つ所有するもの（`feature_*.py`、`self.tray_icon` と `hotkeys()`、`main.py` の `FEATURE_CLASSES` に追加）。抽象基底クラスは意図的に作らない。アイコンを持たない能力は普通のモジュールにする。トレイアイコンは2つに固定し、新しいアイコンを増やさない
- 落ちる書き方（すべて実際に踏んだもの）：COM オブジェクトを関数の外に出さない／`mss.MSS()` を都度作らず `capture_grab._sct()` を通す／`finally` で Qt を触るなら `try` で囲む／外部から叩かれる入口は必ず `try` で受ける（PySide6 はスロット内の例外でプロセスが終わる）／`comtypes.COMError` は `OSError` のサブクラスではない／`ctypes` は `argtypes`・`restype` を必ず指定／COM ポインタを `ctypes.cast` で変換しない（`QueryInterface` を使う。`tests/test_com_gc.py` が見張っている）／COM ポインタを循環参照の中に置かない
- 外から叩ける口（`_build_command_handlers` と MCP）は「常駐しているこのプロセスでなければできないこと」だけに足す。アプリ起動・ファイル操作は足さない。`traytools_send.py` と `hooks/claude_hook.py` は PySide6 を import しない
- 単一起動の判定は名前付きミューテックス（`CreateMutexW`）。`QLocalServer` は外部コマンドの口としてだけ使う。再起動（`_restart`）では後継を起こす前にミューテックスを手放す
- プロセス一覧で常に2つに見えるのは venv の起動スタブの性質で、二重起動ではない（親子関係と起動時刻の差で見分ける）
- 常駐の再起動：巻き添え（付箋・画面ミラー・エージェントループ・スリープ予約）が無いことを `traytools_send.py restart --check` で確かめてからならエージェントが再起動してよい。アプリの「起動」はエージェントがやらない
- 検証は `QT_QPA_PLATFORM=offscreen` で。offscreen で `setMimeData()` を使うと終了時にセグメンテーション違反（139）が出るが実機とは無関係。日本語フォントが豆腐になることがある
- 疑似エージェントループ：UIA だけを使う（キー送信・マウス操作をしない）／既定は dry-run／危険パターン検知で止まる／3層のタイムアウト／キャンセルはファイル方式／監視モードは1周目の送信をスキップ／応答テキストは `FindAll` の並びのまま連結する（並べ替えない）／`wait_until_idle` は idle の連続で判定／`new_response` は送信直前の全文長から切り出す／PC ごとの違いは `settings.json` の `copilot_profiles` で上書き
- `settings.json` は丸ごと書き戻さず、読み直して該当キーだけ差し替える
- photometa は PhotoCuller（private リポジトリ）からも使われている。関数・クラス・引数を消す・名前を変えるときは photo-culler 側の呼び出しを検索し、必要なら両方同時に直す。このリポジトリの CI は PhotoCuller のテストを回さない
- git：コミットメッセージは日本語で、1行目に要約、本文に「なぜそうしたか」。このリポジトリは public で、個人的な除外は `.gitignore` ではなく `.git/info/exclude` へ。push の直前に、追跡対象ファイル・秘密情報らしき文字列・著者（noreply 統一）を毎回確認し、引っかかったら push せずに報告する
- README 側のルール：venv はリポジトリの外（`%USERPROFILE%\.venvs\tray-tools`）に置き、グローバルの `python` ではなく必ず venv の `python.exe` を使う／`settings.json`・`*.lnk`・venv は PC 固有でコミットしない／テストはモジュールごとに `tests/test_*.py` があり、ファイラは `--full` で全件（CI では常に全件）、ExifTool の実書きテストは環境に無ければ skip／MCP サーバーの実装上の約束（SDK を使わない、`stdout` には MCP のメッセージ以外を書かない、常駐が起きていなければ起こしに行かない、引数の不正は `isError` で返す）

### B. Claude（Claude Code）にだけ当てはまるもの（`CLAUDE.md` に残すもの）

- MCP サーバーの登録先が `~/.claude/settings.json` の `mcpServers` であることと、Claude Code の起動ごとにサーバーが立つため起動 0.085 秒・PySide6 を読まないことが効く、という事情（道具の線引き自体は A）
- MCP ツールの annotations（`status` は `readOnlyHint`、`sleep_pc` は `destructiveHint` など）で、Claude 側クライアントの承認の重さを分けている点
- `hooks/claude_hook.py` という Claude Code のフックの受け口そのもの（PySide6 を import しない理由は A と共通）
- README の「Claude Code から道具として呼ぶ」節と、エージェントループ通常 auto モードの起動元が Claude Code / CLI であること、Claude Code の無い業務 PC では監視モードが本命という使い分け
- `CLAUDE.md` という入口自体（Claude Code が自動で読むファイル）。`AGENTS.md` 化する場合は、依頼の前提どおり `CLAUDE.md` から参照する形になる

### C. Muse の環境（Linux VM・GitHub コネクタ）では守れない・意味がないもの

- Windows 実機が前提の確認・操作：常駐の再起動（`restart --check` を含む）、実画面での目視、UIA で Copilot アプリを回すエージェントループ、Teams・Everything・WinMerge・ExifTool の実書き、`SHFileOperationW` の実動作テストなど。Muse の VM は Linux で、ユーザーの PC で動いている常駐プロセスにも触れない
  - 案：Muse 側は純関数テスト・静的チェック・offscreen で回る範囲までを行い、実機でしか確かめられない項目は結果ファイルに「未実施・実機確認待ち」として切り分けて書く。実機確認は Nagato 本人か PC 側の担当に回す
- `CLAUDE.md` の「通常の push は確認なしでよい」：Muse の GitHub コネクタは書き込みごとに承認カードが出るため、そのままでは守れない
  - 案：`AGENTS.md` では書き分ける。「Claude Code は確認なしで push 可。Muse は push 可だが、コネクタの承認カードが確認の代わりになる。1 push = 1 コミットにまとめてカードの回数を減らすこと」
- push 前チェックの git コマンド群（`git diff origin/main..HEAD` など）：コネクタ経由の push では手元の git にその HEAD が無い構成があり、そのままでは実行できないことがある
  - 案：ローカルクローンを `git fetch` してから、同じ観点（変更ファイルの一覧・秘密情報のパターン・著者・追跡対象になっていないこと）で確かめる代替手順を `AGENTS.md` に併記する
- 改行コードのうち「Python の `open(path, 'w')` が `\n` を `\r\n` に変換する」という警告：Windows 版 Python の話で、Linux VM では再現しない（LF を維持すること自体は A として有効）
- プロセス一覧が2つに見える件と、ミューテックス取得の確認コマンド（`python -c "import main; print(main._acquire_single_instance())"`）：知識としては A だが、確認の実行先はユーザーの PC の常駐であり、Muse からは実行できない

### 矛盾・古くなっている記述

- Python のバージョン：`CLAUDE.md` 冒頭は「Python 3.14」だが、README のセットアップでは「`setup.py` は Python 3.10 以上を確認」とある。3.10 で本当に通るのか、3.14 が必須なのかを統一したほうがよい
- venv パスの書き方：`CLAUDE.md` のプロセスの節は `.venvs\tray-tools\...` と相対パスに見える書き方だが、README は一貫して `%USERPROFILE%\.venvs\tray-tools`。`AGENTS.md` 化の際は README 側に統一を
- テストの実行コマンドが README 内で不統一：venv のフルパスで書く節（Teams・スライドショー・photometa・診断）と、素の `python tests\...` で書く節（背景透過・ファイラ）が混在する。README 自身が「グローバルの `python` ではなく必ず venv を」と言っているので、素の `python` 表記は誤解の元。あわせて、`CLAUDE.md` 側にはテストの回し方の統一した記述が無く、`AGENTS.md` に基本コマンドと `--full`・CI の関係を1か所に置く価値がある
- MCP ツール一覧の不整合：README の MCP の節の一覧に `agent_loop` が無いが、エージェントループの節では「Claude Code から MCP 経由でも呼べる（ツール名 `agent_loop`）」とある。一覧への追加漏れと思われる
- M365 Copilot の発言マーカーが未特定で、業務 PC では `agent_loop` の応答切り出しが正しく効かないという制限が `CLAUDE.md` にはあるが、README の業務 PC への移植の節には書かれていない。README だけ読むと「同じコードがどちらでも動く」と読めてしまう
- push 前チェックの対象：`CLAUDE.md` の確認リストは `settings.json` と `hook.log`・`action.log`・`crash.log`・`error.log` だが、README で「個人の使用履歴なので追跡対象外」と明記されている `copilot_loop.log` がリストに入っていない。チェックリストへの追加候補
- README のエージェントループの節に「### 安全機構」の見出しが2つ連続している（内容は1つ分だけ）

## 試運転についてのメモ

- 依頼の1行は分かりやすかった。リポジトリ・依頼書のパス・ブランチ・base_sha がすべて入っていて、受け取ってすぐ着手できた。結果ファイルの書式が依頼書に固定されていたのも迷いがなかった
- 作業中に main が進んだ場合の扱いが未定義だった。今回は着手直前に Claude 側の別コミット（`0c14c3c`、進捗パネルの折りたたみ）が main に入っていて、main の HEAD が base_sha からずれていた。`CLAUDE.md` は base と同一、`README.md` はその節まわりのみの差分であることを blob の SHA で確認し、下調べは指定どおり base_sha 版で行い、ブランチは現在の main から切った。base に固定したいのか、現在の main からでよいのかを依頼書に1行あると、確認の往復なしに進められる
- GitHub コネクタのブランチ作成は起点をブランチ名でしか指定できず、base_sha ぴったりからは切れない。厳密な base 固定が必要な回では、この点が制約になる
- `approvals` の数え方が依頼書で未定義だった。今回は書き込み系のみを数えて2回とした（ブランチ作成1回＋push 1回）。読み取り（`get_file_contents`・`list_commits`）は読み取り権限で、承認カードは出なかった
- 次から変えるとよい点：依頼書に「main が進んでいた場合の扱い」と「approvals の数え方（読み取りを含むか）」の2点を足すと、記録まで含めて完全に揃う
