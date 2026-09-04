# tools/

`tray-tools` 本体に組み込むほどではないけれど、環境調査や新しいアプリへの移植の下ごしらえに便利な調査系スクリプトを置く場所。

## `uia_probe.py`

Copilot 系アプリの UI Automation ツリーを覗いて、`copilot_loop.py` の**プロファイル**の候補を出してくれる。**業務PCで M365 Copilot 版に対応するとき**に使う想定。

### 前提：業務PCからはデータを持ち出せない

画面に見えているものを読み上げて伝えるしかない。そのため出力の最後に必ず**「ここから下だけ読み上げれば足ります」のまとめ**が出る。長い一覧はその上に残してあるので、迷ったときだけ戻ればよい。この制約が、以下の設計をだいたい決めている。

### 使い方（この順に叩く）

```powershell
# 1. 対象を探す。可視の窓を class / exe / title で全部並べる
python R:\claude\tray-tools\tools\uia_probe.py --survey

# 2. 見つけた exe で絞って調べる。30秒のあいだに実際に1往復させること
python R:\claude\tray-tools\tools\uia_probe.py --exe "M365Copilot.exe" --watch 30

# 3. 仕込んだプロファイルで実際に掴めるかを診断する
python R:\claude\tray-tools\tools\uia_probe.py --check

# 4. 発言マーカーの有無を突き止める（1往復の前後を比べる）
python R:\claude\tray-tools\tools\uia_probe.py --markers
```

`--out FILE` を付ければ画面と同じ内容をファイルにも残せる。

### 出力の読み方

- **[窓]** — HWND / class / exe / レンダラ HWND / 子孫数。**子孫数が2桁以下ならツリーが起きていない**。対象アプリを一度クリックして前面にしてから再実行する。
- **[下段のボタン]** — 入力欄まわりのボタン名。`--watch` を付けて実際に送信すると遷移が見えて `send_button` と `busy_button` が分かる。**常に出ているボタンは自動で捨てる**ので、候補は2〜3個まで落ちる（十数個から選ばせない）。
- **[入力欄候補]** — `AutomationId` 付きの Edit / ComboBox。
- **[発言マーカー候補]** — 2回以上出てくる短い Text。会話の往復ごとに現れるものが手掛かり。

### 実測値（2026-09-05 時点）

| | 手元PC | 業務PC |
|---|---|---|
| exe | `mscopilot.exe` | `M365Copilot.exe` |
| class | `Chrome_WidgetWin_1` | `Microsoft 365 Copilot Host` |
| 入力欄 aid | `userInput` | `m365-chat-editor-target-element` |
| 送信 | `メッセージの送信` | `送信` |
| 回答中 | `メッセージの割り込み` | `生成を停止する` |
| 発言マーカー | `あなたの発言` / `Copilot の発言` | **未特定** |

### そのあとやること

出てきた値を `copilot_loop.BUILTIN_PROFILES` に足すか、`settings.json` の `copilot_profiles` に書く（**業務PCでは後者が楽**。コードを配らずに直せるし、`settings.json` は git 管理外なので PC ごとに違う値を持たせても衝突しない）。同じ `name` なら設定側が勝つ。

そのあと `--check` で全項目 OK になることを確かめる。

### 発言マーカーが見つからない場合

M365 Copilot は `あなたの発言` に相当する Text を出していない。マーカーが空でも次は動く。

- **状態表示（`copilot_watchdog`）** — マーカーを使わないので影響なし
- **`agent_loop` の通常モード** — 送った本文を目印に切り出す（`strip_echoed_prompt`）ので動く
- **`agent_loop` の監視モード** — 人が手で投稿した本文をこちらが知らないため、**未解決**

`--markers` は、1往復の前後で会話の全文を比べ、増えた部分の頭200文字だけを見せる。`[自分の発言][区切り?][応答]` の順に並ぶので、境目に毎回同じ短い語が挟まっていればそれがマーカー。何も挟まっていなければマーカー方式は使えない、と結論できる。

### なぜレンダラを起こす必要があるのか

Chromium は支援技術を検出するまで、アクセシビリティツリーを作らない（レンダラの CPU を守るための最適化）。素で覗くと窓枠だけ（子孫が10個ちょっと）しか見えない。レンダラの HWND へ `WM_GETOBJECT` を投げると「読みに来る人が居る」と判断してツリーを作り、子孫が数百個に増える。実測: 11個 → 371個（Copilot 版）。プローブでも `agent_loop` でも同じ処理を毎回やっている。

### 依存

- `comtypes`（venv に入っている）
- `PySide6` は不要。プローブは軽い。
