# tools/

`tray-tools` 本体に組み込むほどではないけれど、環境調査や新しいアプリへの移植の下ごしらえに便利な調査系スクリプトを置く場所。

## `uia_probe.py`

Copilot 系アプリの UI Automation ツリーを覗いて、`copilot_loop.py` の `SELECTORS` の候補を出してくれる。**業務PCで M365 Copilot 版に対応するとき**に使う想定。

### 使い方

```powershell
# 対象アプリを前面にしてから
python R:\claude\tray-tools\tools\uia_probe.py --title "Copilot"

# 何が回答中に切り替わるかを見る（実際にメッセージを送りながら20秒眺める）
python R:\claude\tray-tools\tools\uia_probe.py --title "Copilot" --watch 20

# 対象の窓が分からないとき（Chromium 系すべてを列挙）
python R:\claude\tray-tools\tools\uia_probe.py --any
```

### 出力の読み方

3ブロック出る。

1. **[窓]** — HWND / class / レンダラ HWND / 子孫数。**子孫数が2桁以下ならツリーが起きていない**。対象アプリを一度クリックして前面にしてから再実行してほしい。Chromium はバックグラウンドではレンダラを廃棄することがある。
2. **[下段のボタン]** — 入力欄まわりのボタン名。ここに送信・停止・「会話する」相当のボタン名が並ぶ。`--watch` を付けて実際にメッセージを送信すると、`メッセージの送信` → `メッセージの割り込み` → `Copilot と会話する` のような**遷移**が見えて、`send_button` と `busy_button` を突き止められる。
3. **[入力欄候補]** — `AutomationId` 付きの Edit / ComboBox。Copilot 版では `userInput` という AutomationId が付いていた。M365 版は違う可能性が高い。

### そのあとやること

出てきた候補を `R:\claude\tray-tools\copilot_loop.py` の `SELECTORS` に貼って、`agent-loop status` で単に接続できることを確かめる。応答本文の切り出し（`assistant_marker` / `user_marker`）は、`copilot_loop.Copilot().document_text()` の出力を見ながら特定するとよい。

### なぜレンダラを起こす必要があるのか

Chromium は支援技術を検出するまで、アクセシビリティツリーを作らない（レンダラの CPU を守るための最適化）。素で覗くと窓枠だけ（子孫が10個ちょっと）しか見えない。レンダラの HWND へ `WM_GETOBJECT` を投げると「読みに来る人が居る」と判断してツリーを作り、子孫が数百個に増える。実測: 11個 → 371個（Copilot 版）。プローブでも `agent_loop` でも同じ処理を毎回やっている。

### 依存

- `comtypes`（venv に入っている）
- `PySide6` は不要。プローブは軽い。
