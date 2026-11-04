@AGENTS.md

# tray-tools 作業ルール（Claude 固有）

**共通ルールは `AGENTS.md` が正本です。** このファイルには Claude（Claude Code）にだけ当てはまることだけを残してあります。共通ルールを探すときは、まず `AGENTS.md` を読んでください。

詳しい仕様は `README.md`（このリポジトリのいちばん厚い文書）にあります。

## `CLAUDE.md` という入口自体

- `CLAUDE.md` は Claude Code が自動で読むファイルです。冒頭の `@AGENTS.md` は、Claude Code がこの書き方で別ファイルを読み込むための参照です。
- 共通ルールの正本を `AGENTS.md` に置くのは、Claude Code と Muse が同じルールで作業できるようにするためです。共通ルールを直すときは `CLAUDE.md` ではなく `AGENTS.md` を直してください。

## MCP サーバーの登録と起動

- MCP サーバーの登録先は `~/.claude/settings.json` の `mcpServers` です。
- Claude Code の起動ごとにこのサーバーが立つため、起動が軽いことが効きます。**PySide6 を読まないので、起動は実測 0.085 秒**です（登録の説明は `README.md` の「Claude Code から道具として呼ぶ」節にもあります）。
- 道具の線引き自体（載せてよいのは「常駐しているこのプロセスでなければできないこと」だけ）は共通ルールで、`AGENTS.md` の「外から叩ける口」と「MCP サーバーの実装上の約束（共通部分）」にあります。

## MCP ツールの annotations

- MCP ツールの annotations（`status` は `readOnlyHint`、`sleep_pc` は `destructiveHint` など）で、Claude 側クライアントの承認の重さを分けています。
- `annotations` はクライアントが承認の重さを決める材料です。読むだけのものに `readOnlyHint` を付けてあるので、都度確認せずに使う設定にしやすくなっています。逆に `sleep_pc` には `destructiveHint` を付けてあります —— **作業中に寝られるのがいちばん困る**ためです。

## Claude Code のフックの受け口

- `hooks/claude_hook.py` は Claude Code のフックの受け口そのものです。
- `hooks/claude_hook.py` が PySide6 を import しない理由は共通ルールと共通です（`AGENTS.md` の「外から叩ける口」参照）。

## Claude Code から道具として呼ぶこと・起動元

- `README.md` の「Claude Code から道具として呼ぶ」節が、Claude Code から MCP サーバー経由で常駐を呼ぶ使い方の説明です。
- エージェントループの通常 auto モードの起動元は Claude Code / CLI です。**Claude Code の無い業務 PC では監視モードが本命**という使い分けです（モード自体の仕様は `AGENTS.md` の「疑似エージェントループ」にあります）。
