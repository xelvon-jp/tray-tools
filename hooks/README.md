# Claude Code フック連携

Claude Code のフックから tray-tools を鳴らし、**画面を見ていなくても次のことに気づける**ようにする中継です。

- 権限確認で止まっている（承認するまで先へ進まない）
- 応答が終わった / 途中で失敗した

新しい常駐は増えません。既にある `beep` / `pushover` の口を、フックから叩くだけです。

## 中身

| ファイル | |
|---|---|
| `claude_hook.py` | 本体。標準ライブラリ＋`ctypes` だけで動く（**PySide6 は読まない**） |
| `config.json` | 閾値と入切。無くても既定値で動く |

## イベントとの対応

`~/.claude/settings.json`（または各プロジェクトの `.claude/settings.json`）の `hooks` に、`type: "command"` のハンドラとして登録します。**設定の追記はユーザーが行ってください。**

| フックイベント | 渡す引数 | 動き |
|---|---|---|
| `UserPromptSubmit` | `start` | 開始時刻を控えるだけ。**音は鳴らない** |
| `Stop` | `stop` | 掛かった時間が `min_seconds` を超えたときだけ `beep done` |
| `StopFailure` | `fail` | 時間に関わらず `beep error` |
| `Notification`（matcher `permission_prompt`） | `ask` | `beep ask` |

呼ぶ Python は **venv のもの**（`C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe`）にしてください。理由は下の「踏みやすい罠」に書いてあります。

`timeout` は必ず縮めてください（**既定は 600 秒**です）。名前付きパイプが詰まったときに、応答が終わってから10分待たされることになります。`10` で足ります。

## 設定（`config.json`）

| キー | 既定 | |
|---|---|---|
| `enabled` | `true` | 全体の入切。`false` にすれば、settings.json のフック定義を消さずに黙らせられる |
| `min_seconds` | `60` | これより短い応答では鳴らさない |
| `away_seconds` | `180` | 最後のキー/マウス操作からこれだけ経っていたら「離席中」 |
| `pushover_when_away` | `true` | 離席中はスマホへも送るか |
| `pushover_min_seconds` | `300` | スマホへ送るのはこれ以上かかったときだけ。音より敷居を上げてある |
| `pushover_include_excerpt` | `false` | スマホの本文に応答の書き出しを載せるか |
| `excerpt_chars` | `120` | 載せる場合の長さ |
| `beep_on_done` / `beep_on_error` / `beep_on_ask` | `true` | 種類ごとの入切 |
| `sound_done` / `sound_error` / `sound_ask` | `done` / `error` / `warn` | 鳴らす音（`beep.py` の名前: `ok` `done` `warn` `error` `ask`） |
| `ask_repeat` | `2` | 確認待ちを何回鳴らすか |
| `ask_repeat_interval` | `0.35` | 繰り返すときの間隔（秒） |

### 確認待ちに `ask` を使っていない理由

**Windows の既定のサウンド設定では「質問（`SystemQuestion`）」に音が割り当てられていません。** そのまま `beep ask` を使うと、いちばん気づきたい「止まっている」が**無音**になります（このPCでも実際に空でした）。

さらに既定では「情報（`SystemAsterisk`）」と「警告（`SystemExclamation`）」が**同じ wav** を指しているので、音色では完了と区別が付きません。そこで**回数**で分けています（完了は1回、確認待ちは2回）。

コントロールパネルのサウンドで「質問」に音を割り当てたなら、`sound_ask` を `ask` に、`ask_repeat` を `1` に戻してかまいません。

割り当てを確かめるには:

```powershell
(Get-ItemProperty 'HKCU:\AppEvents\Schemes\Apps\.Default\SystemQuestion\.Current').'(default)'
```

## 設計上、外せない点

### 必ず終了コード 0 で終わる

**`Stop` フックが 2 を返すと「停止をブロック」と解釈され、Claude が止まれなくなります。** 音を鳴らすだけのフックでそれが起きるのは事故でしかないので、`main()` は何があっても 0 を返します（理由は標準エラーへ書くだけ）。

### 経過時間で絞る

**`Stop` は毎ターン発火します。** 相槌のような短い応答でも鳴るので、素で繋ぐと必ず鬱陶しくなって数日で外すことになります。`UserPromptSubmit` で開始時刻を控え、`Stop` 側で差を見て閾値を超えたときだけ鳴らします。

控えが**無い**ときは鳴らしません。フックを入れた直後や、再開したセッションの1ターン目がこれに当たります。「分からない」を「0秒」とみなして鳴らすより、静かな方に倒しています。

控えはセッションごとに1ファイル（`%TEMP%\tray-tools-hooks\<session_id>.json`）です。複数セッションを並行させても混ざりません。`session_id` は外から来る値なので、ファイル名に使う前に `[^A-Za-z0-9_.-]` を潰します（`..\` が来て別の場所を書きに行かないため）。24時間より古い控えは、次に `start` が走ったときに片付けます。

### tray-tools が起きていなければ何もしない

`traytools_send.send()` は未起動なら本体を起動して最大6秒待ちますが、それは**あふｗから呼ぶときの作法**です。フックで数秒止まるのは困りますし、音を鳴らすためだけに常駐を起こすのも筋が違います。ここでは `_exchange()` を直に使い、パイプが無ければ黙って諦めます（実測 0.17 秒で戻ります）。

応答は 2 秒で見切ります。`traytools_send` は `pushover` に最長25秒待ちますが、こちらは応答を使わないので待つ意味がありません。**パイプへ書けた時点で本体は受け取っています。**

### PySide6 を読まない

フックは毎ターン走ります。Qt の import だけで1秒近くかかるので、そのぶん全ターンが遅くなります。`traytools_send.py` と `mouse_jiggler.py` はどちらも標準ライブラリ（`ctypes`）だけで動くので、この2つまでは読んでよいことにしています。

実測: `start` が **0.17 秒**、`stop`（実際に鳴らすところまで）が **0.20 秒**。

## 踏みやすい罠

- **呼ぶ Python は venv のものにする。** `claude_hook.py` も `traytools_send.py` も標準ライブラリだけで動きますが、`traytools_send` は**未起動時に本体を起動する経路**で `sys.executable` から `pythonw.exe` を割り出します。この中継はその経路を使わないものの、同じモジュールを読む以上、素の Python を指すと将来の変更で足をすくわれます。
- **`keep-awake` を `SessionStart` に付けない。** 全セッションで抑止が掛かります。プロジェクト単位の `.claude/settings.json` か、明示的な呼び出しに寄せてください。
- **フックの失敗はセッションを止めません**（exit 0 以外は non-blocking error）。ただしログには出るので、パス間違いには早めに気づけます。

## 手で試す

```bash
C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe R:\claude\tray-tools\hooks\claude_hook.py ask
```

標準入力に JSON が来なくても動きます（`session_id` が無いので経過時間は使えませんが、`ask` と `fail` はそもそも時間で絞りません）。
