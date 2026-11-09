# 窓口 (front_desk)

Muse からの依頼を Worker 経由で受け取り、実行モデル目安 (tier) に
応じて実行コマンド (Claude Code セッション) を起動し、結果を返す
小さな常駐プログラム。設計は collab-workspace の
`docs/front-desk-design.md`、Worker 側の契約は
`docs/front-desk/worker-patch/README.md`。

配備係 (deploy_daemon) と同じ作法で作ってある: 標準ライブラリのみ、
状態は `%LOCALAPPDATA%\traytools\front-desk`、ログは JSONL、
鍵は環境変数から、停止は合図ファイル。

## 動き

1. 60秒ごと (設定可) に Worker の `GET /requests` で未処理の依頼を取る
2. 種別 (kind) が許可外なら `rejected` で終端 (第1段は `inspect` のみ)
3. `running` を書いてから、tier ごとの実行コマンドを起動し、
   依頼本文を標準入力で渡す
4. 終了したら結果を報告する (`done` / `failed` / 時間切れは `blocked`、
   終了コード非0は `failed`)。この報告 (`terminal: true`) が ack
5. 自分の状態を `POST /desk-state` へ送る (変化時＋ heartbeat)。
   Muse はこれで窓口の生存と処理中の依頼を読める

結果の要約 (summary) は「出力の最後の空でない行」を使う。
依頼文では、実行側に最後の行へ結論を1行で書くよう指示する前提。
詳細 (detail) は出力の末尾 4000 文字。

## 二重実行と中断の扱い

- 処理済みの記録 (`processed.json`) が二重実行を防ぐ。結果の報告に
  失敗しても処理済みに記録する (副作用のある実行をやり直さないため)。
- 実行開始だけ記録されて終わっていない依頼は、再起動後の最初の
  見回りで `interrupted` として終端させる (再実行はしない)。

## 設定 (config.json)

```json
{
  "worker_base_url": "https://<worker の基底 URL>",
  "poll_interval_sec": 60,
  "exec_timeout_sec": 1800,
  "state_heartbeat_polls": 10,
  "allowed_kinds": ["inspect"],
  "executor_commands": {
    "haiku": ["claude", "-p", "--model", "haiku"],
    "opus": ["claude", "-p", "--model", "opus"]
  },
  "workdir": "R:\\claude"
}
```

- `executor_commands` の具体的な作法 (Claude Code のヘッドレス起動)
  は、PC 側での実地確認 (ロードマップ A7) の後に確定する。未設定の
  tier の依頼は `failed` (未設定) で返る。
- 窓口鍵は設定に書かず、環境変数 `FRONT_DESK_KEY` で渡す。

## プレイブック (モデルなし実行)

tier は「実行エンジン」の指定で、`haiku` / `opus` はモデル、
`playbook` はモデルを起動せず `front_desk.playbooks` のコードが
実行する (A9 の第2段)。依頼の body は JSON で
`{"playbook": "<id>", "params": {...}}` と書く。kind は当面
`ops` だけ。

```json
{
  "allowed_kinds": ["inspect", "ops"],
  "allowed_playbooks": ["traytools_sync"],
  "playbook_settings": {
    "traytools_sync": {"repo_path": "R:\\claude\\tray-tools"}
  }
}
```

- `allowed_playbooks` に無い id は実行されず `rejected` で返る
  (既定は空 = 1つも実行できない)。
- `traytools_sync`: 対象リポジトリへ対象ブランチを ff-only で
  取り込み、traytools_send.py の口で再起動し、HEAD と生存を
  検証する。作業ツリーが dirty・別ブランチ・再起動の吹き添え
  確認が NG のときは、何も変えず `blocked` で返す。反映後の
  失敗では、ツリーが依然 clean な場合だけ巻き戻しを1回試す。
- 設計と有効化の手順は claude-bridge の playbook-spec.md。

## 起動と停止

```
python -m front_desk --config config.json          # 常駐
python -m front_desk --config config.json --once   # 見回り1回
```

停止は `base_dir` に `stop` ファイルを置く (見回りの合間に止まる)。
常駐化はタスクスケジューラ＋ pythonw を想定 (配備係と同じ)。
単一インスタンスの保証は置き場側の運用に委ねる (配備係と同様)。
