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

困りどころ別の単発モードもある。

```powershell
# いま触っている部品を掴む（下記の --spot）
python R:\claude\tray-tools\tools\uia_probe.py --spot

# 本番の状態判定の遷移を記録する（下記の --record）
python R:\claude\tray-tools\tools\uia_probe.py --record 120
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

### いま触っている部品を掴む（`--spot`）

「このボタンの実物の名前と aid が知りたい」というピンポイントの用事のためのモード。ツリー全体から探し直さずに済む。

1. 走らせると3秒のカウントダウンが始まる（`--spot-wait` で変更可）
2. その間に、対象アプリで調べたい部品をクリックするか、マウスを乗せたままにする
3. フォーカス要素とマウス下の要素について、種類・名前・aid・class・持っているパターン（Invoke / Value など）と、**祖先の連なり**が出る。aid が無い要素でも、どの段に aid が付いているかが見える
4. 最後に読み上げ用のまとめ（2つの要素の aid / 名前 / class）が出る

フォーカスは奪わない（起こすのは `WM_GETOBJECT` だけ）ので、掴んだ時点の状態はそのまま。生成中の停止ボタンの実物を掴む、といった使い方もできる。

### 状態判定の遷移を記録する（`--record`）

`copilot_loop` の状態判定が本番でどう振る舞ったかを切り分けるためのモード。`--watch`（ボタン集合を見るプローブ独自の見方）と違い、こちらは**本番の `status_snapshot` と同じ材料・同じ規則**で状態を計算し、遷移を時刻つきで記録する。あわせて会話の長さ（Text 要素の名前の合計）も追う。

1. Copilot を開いた状態で走らせる。記録中に実際に1往復させる（Ctrl+C で早めに終われる）
2. 状態かボタン集合が変わるたびに `[mm:ss] busy ボタン=[…] 会話=N字` の形で流れる。本文が伸びている間は進みも薄く流れる
3. 最後に読み上げ用のまとめ：遷移の列（`idle(12秒) → ready(3秒) → busy(48秒) → idle`）、busy を見たか、切り替わり回数、会話の長さの増減、入力欄を掴めたか

切り分けの例：生成していたのに busy が一度も出ない → `busy_button` の名前が実物と合っていない（`--spot` で実物を掴む）。切り替わりが多すぎる → 状態がちらついていて完了待ちの settle 判定が誤判定しやすい。idle に戻ったのに会話が最大長より短い → 表示が縮んでいて、長さで応答を切り出す側に影響する。

### Teams の一人会議（`--teams`）

`teams_meeting.py`（一人会議の立ち上げ）の手順を業務PCで合わせるためのモード。**読むだけで、何も押しません。**

```
python R:\claude\tray-tools\tools\uia_probe.py --teams --watch 300
```

1. Teams を開いた状態で走らせる。最初に見えていた要素は記録しない
2. 見張っているあいだに、手で「今すぐ会議 → 会議を開始 → 今すぐ参加 → その他 → 録画と文字起こし → 文字起こしの開始」まで進める（Ctrl+C で早めに終われる）
3. 新しく出たボタン・メニュー項目・ダイアログが、段階（メイン／参加前／会議中）つきで流れる。文字起こし・録画に関わるものには ★ が付く
4. 最後に `teams_meeting.steps` に貼れる JSON の断片（このPCの中でコピーする）と、「ここから下だけ読み上げれば足ります」のまとめ（★の候補・既定の手順で見えなかった要素・段階ごとの主なボタン）が出る

断片の経路（押す順）は推測です。UIA からは「どれが押されたか」が分からないので、既定と同じ「その他 → 録画と文字起こし → 文字起こしの開始」の形に、実際に出た名前を当てはめています。

### なぜレンダラを起こす必要があるのか

Chromium は支援技術を検出するまで、アクセシビリティツリーを作らない（レンダラの CPU を守るための最適化）。素で覗くと窓枠だけ（子孫が10個ちょっと）しか見えない。レンダラの HWND へ `WM_GETOBJECT` を投げると「読みに来る人が居る」と判断してツリーを作り、子孫が数百個に増える。実測: 11個 → 371個（Copilot 版）。プローブでも `agent_loop` でも同じ処理を毎回やっている。

### 依存

- `comtypes`（venv に入っている）
- `PySide6` は不要。プローブは軽い。

## `com_gc_harness.py`

「GC → comtypes の `__del__` → `Release` で access violation」を、常駐を使わずに再現・調査する器。常駐が使う COM の**読み取り**経路（マイクのミュート状態・音量・既定デバイス・デバイス列挙・エクスプローラの窓列挙、任意で UIA）を、メイン＋別スレッド2本で回し、合間に小さな循環を作って GC を誘発する。音量・既定デバイス・ミュートは変えない。

```powershell
# 調査モード。循環 GC に回ってきた COM ポインタを com_gc_probe.log に記録する（落ちない）
python R:\claude\tray-tools\tools\com_gc_harness.py --probe

# 経路を絞る（mic / audio / list / explorer / uia）
python R:\claude\tray-tools\tools\com_gc_harness.py --probe --only mic

# 直す前の mic_control の書き方（ctypes.cast）。調査モードなしで回すと落ちる
python R:\claude\tray-tools\tools\com_gc_harness.py --only mic_legacy
```

`com_gc_probe.log` の `GARBAGE_COM` の行が見つかった COM ポインタ。`created at` が作った場所、`抱えていたもの` が循環の中身。回帰は `tests/test_com_gc.py` で見ている。
