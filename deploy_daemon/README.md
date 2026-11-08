# 配備係（deploy_daemon）

郵便受け Worker の「変更の受け取り口」を見回り、届け出を受け取り、GitHub から
変更を取り、検証用コピーでテストし、実チェックアウトへ反映し、常駐を安全な
再起動インターフェース経由で再起動し、生存確認とロールバックまでを行う、
PC 側の小さな独立プログラム。設計の正本は collab-workspace の
`docs/system-spec.html`（第2・5・6・8節）。

- tray-tools 常駐には組み込まない（再起動の外側にいる必要があるため）。
  常駐へは `traytools_send.py restart --check` → `restart` の口だけで触る。
- 標準ライブラリのみで動く。トレイアイコンは持たない。

## 作業場と反映の考え方

- 作業場は **配備係専用のクローン**（`<base>/staging/<リポジトリ名>`）。
  ここに変更を取り、ここでテストする。
- ユーザーの作業用チェックアウトへの反映は **fast-forward のみ**。
  未コミット変更などで汚れていたら、反映せず止めて報告する（CONFLICT）。
- 反映のあとは **HEAD が届け出の ref と一致することを必ず確かめる**。
  ff-only は ref が現在位置の祖先だと何もせず成功するため、一致しなければ
  反映していない（CONFLICT）として報告する。no-op や後戻りを成功にしない。
- 反映直前の HEAD は記録しておき、再起動・生存確認に失敗したら
  そこへロールバックする。

## 状態機械

```
received → staged → tested → [awaiting_apply_approval] → applied
         → restarted → verified → reported

失敗・保留側の終端:
  rejected        常時承認の範囲外（リポジトリ／ブランチ／ファイル）
  test_failed     テスト失敗。実物へは反映せず、検証用コピーを破棄（仕様 A2）
  conflict        ff できない／作業用チェックアウトが汚れている（仕様 A7）
  held            反映済みだが再起動ブロッカーが続き、再起動を保留
  rolled_back     再起動・生存確認の失敗から、直前の HEAD へ戻して復旧（仕様 A3）
  rollback_failed ロールバック後の再起動・生存確認も失敗。人間待ち
  fetch_failed    変更（ref）を上限回数まで待っても取り込めなかった

終端でない状態:
  deferred                 ref がまだ見えない等の一時的な待ち。次の見回りで再試行
  awaiting_apply_approval  テスト合格後、承認シグナルを待つステージ
```

再起動は、通常経路でもロールバック経路でも、**ブロッカー（編集中など）が
ある間は実行しない**。`restart --check` を間隔を空けて確かめ直し、
`blocker_retries` を超えたら保留（held／rollback_failed）にして報告する。

## 反映モードと承認シグナル

第1段の既定は `apply_mode = "approval"`。テスト合格後にステージで止まり、
承認シグナルが来て初めて反映する。

- 承認シグナル： `<base>/approvals/<届け出ID>.approve` という空ファイル。
  置かれたことを次の見回りで検知して反映へ進む。承認は使い切り（反映時に消す）。
- `apply_mode = "auto"` にすると、承認なしで全面自動になる。
  切り替えは config.json の `apply_mode` か環境変数 `DEPLOY_DAEMON_APPLY_MODE`。

## 受け取り口の契約（Worker パッチ側と合わせる）

基底 URL は設定の `intake_url`（例: `https://<worker>/intake`）。
認証はどちらの向きもヘッダ **`x-deploy-key: <キー>`** の1本。
キーは環境変数 `DEPLOY_DAEMON_INTAKE_KEY`（config.json の `intake_key_env` で
変数名を変更可）からだけ読み、ファイルには書かない。

### 未処理の届け出の取得

```
GET {intake_url}/deliveries
-> 200 {"deliveries": [ <届け出>, ... ]}
```

終端の報告（後述）が済んだ届け出は返らない。届け出の形式：

```json
{
  "id": "dlv-20261109-01",
  "repo": "tray-tools",
  "branch": "muse/example",
  "ref": "40桁のSHA",
  "summary": "変更の要約（平易な言葉）",
  "created_at": "2026-11-09T01:00:00+09:00",
  "paths": ["任意：変更ファイルの宣言"],
  "bundle_ref": null
}
```

変更そのものは GitHub へ push 済みで、届け出は参照だけを持つ
（仕様書 第5節：合図→納品の順。そのため ref がまだ見えない届け出は
deferred として次の見回りで再試行する）。`bundle_ref` は将来の
フォールバック経路用に予約した参照で、第1増分では未対応（rejected で報告）。

### 処理結果の報告（ack）

```
POST {intake_url}/deliveries/{id}/report
body: {
  "state": "verified など",
  "ok": true/false,
  "terminal": true/false,
  "summary": "平易な言葉の結果",
  "detail": "失敗ログの末尾など",
  "at": "ISO時刻"
}
-> 200 {"ok": true}
```

`terminal: true` の報告が ack になり、その届け出は以後 GET で返らない。
途中の状態（received／deferred／awaiting_apply_approval）は
`terminal: false` で報告され、Worker 側は最新だけを保持すればよい。

## 設定

基底フォルダは Windows では `%LOCALAPPDATA%\traytools\deploy-daemon\`
（環境変数 `DEPLOY_DAEMON_BASE_DIR` で変更可）。そこの `config.json` を読み、
環境変数があればそちらを優先する。

| キー (config.json) | 環境変数 | 意味 |
|---|---|---|
| `checkout_dir` | `DEPLOY_DAEMON_CHECKOUT_DIR` | 実チェックアウト（必須） |
| `intake_url` | `DEPLOY_DAEMON_INTAKE_URL` | 受け取り口の基底 URL |
| （`intake_key_env` で変数名指定） | `DEPLOY_DAEMON_INTAKE_KEY` | 受け取り口のキー |
| `allowed_repos` | ― | 常時承認の対象リポジトリ（既定 `["tray-tools"]`） |
| `allowed_branches` | ― | 対象ブランチのパターン（既定 `["main", "muse/*"]`） |
| `allowed_path_prefixes` | ― | 対象パスの前方一致（既定は全て可） |
| `remotes` | ― | リポジトリ名 → clone 元 URL/パス |
| `test_commands` | `DEPLOY_DAEMON_TEST_COMMAND` | 検証用コピーで実行するコマンド列 |
| `apply_mode` | `DEPLOY_DAEMON_APPLY_MODE` | `"approval"`（既定）／`"auto"` |
| `poll_interval_sec` | `DEPLOY_DAEMON_POLL_INTERVAL` | 見回り間隔（既定60秒） |
| `blocker_wait_sec` / `blocker_retries` | ― | ブロッカー待ちの間隔と上限 |
| `python_executable` | ― | `traytools_send.py` を呼ぶ実行系（既定 `python`。実機では venv の python.exe を想定） |
| `send_script` | `DEPLOY_DAEMON_SEND_SCRIPT` | 再起動スクリプトの差し替え（既定は `checkout_dir` 内の `traytools_send.py`。初回テストでスタブに差し替えるときだけ指定する） |

スタブを使う場合の応答仕様：`restart --check` は「差し支えありません」を含む出力で終了コード 0、`restart` と `status` は終了コード 0 を返すこと（配備係はこの形で本物と同じに読みます）。

テストコマンドが未設定の場合、安全のため「テスト失敗」として扱い反映しない。

### 常時承認の意味

設定に書かれた範囲は、ユーザーが事前に承認した範囲として扱う
（仕様書 第8節）。範囲外は必ず止めて報告する。承認カードの代わりに、
事後の監査として次の2つを残す。

- `<base>/operations.jsonl` — 全操作の追記型 JSONL ログ
- `<base>/state.json` — 配備係自身の状態と届け出ごとの状態
  （ダッシュボード／ブリッジ用。「自分の状態だけを書く」原則に従う）

### 停止方法

`<base>/stop` というファイル（中身は不問）を置くと、新規受け取りを止める。
配備係自身の再起動は自動ではしない（仕様書 A6）。

## 実行

```
python -m deploy_daemon --once    1回だけ見回る
python -m deploy_daemon           見回りループ
```

実チェックアウトと `intake_url` が未設定なら、安全のため起動しない。

## テスト

```
python3 tests/test_deploy_daemon.py
```

輸送はインメモリのフェイク、git は一時リポジトリの実物、再起動と生存確認は
フェイクで回す。Windows 実機でしか確認できない項目は別途実機確認に回す。
