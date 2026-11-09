# LAN Drop 本番切り替え手順(単体版 → tray-tools 統合版)

実機(受信 PC)での切り替え手順。実施は Claude(Windows 実機側)。
所要は15分ほど。受信済みファイルには影響しない(保存先は共有のまま)。

## 0. 現状を控える

- 旧設定ファイルの場所(通常 `Desktop\claude\lan-drop\config.toml`)と、
  その中の `save_dir` / `port` を控える。
- 旧トレイが動いていること、iPhone から送れることを確認しておく。

## 1. 旧い LAN Drop を止める

- 旧トレイのメニューから終了する。
- ポート(既定 8000)が空いたことを確認する
  (`Test-NetConnection -ComputerName 127.0.0.1 -Port 8000` が失敗側になる)。

## 2. 統合版の準備

```powershell
cd R:\claude\tray-tools
git pull   # main に LAN Drop 統合(第1〜3段)が入っていること
& "C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe" -m pip install -r requirements-landrop.txt
& "C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe" -m landrop.startup_link status
```

status で「登録: あり(旧向き先)」「設定: 旧の場所」がどう出るかを見る。

## 3. 登録を作り直す(設定を引き継ぐ)

```powershell
& "C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe" -m landrop.startup_link install --from-config "C:\Users\yotan\Desktop\claude\lan-drop\config.toml"
```

- Startup の `LAN Drop.lnk` は同じ名前なので、新しい向き先
  (tray-tools の venv の pythonw)で上書きされる。
- 設定は**コピー**される(元は残る)。既に統合版の設定が解決できる場合は
  コピーは起きず、その設定が使われる。
- 出力の「設定:」行で、トークンあり・保存先が旧と同じことを確認する。

## 4. 起動と確認

```powershell
Start-Process "C:\Users\yotan\.venvs\tray-tools\Scripts\pythonw.exe" -ArgumentList "-m","landrop","--tray" -WorkingDirectory "R:\claude\tray-tools"
```

- トレイに LAN Drop のアイコンが出る。
- iPhone から写真を1枚送る → 保存先に届く。
- バルーン通知が出て、クリックで受信フォルダが開く(第2段の通知差し替え分)。
- テキスト送信 → ポップアップが出る。
- 案内ページ(QR)が開けて、トークンが旧と同じであること。

## 戻し方

1. 統合版のトレイをメニューから終了する。
2. 登録を消す:
   `& "C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe" -m landrop.startup_link remove`
3. 旧い LAN Drop を起動する(単体時代の start_tray.bat)。
   旧の .lnk が要る場合は、旧トレイのメニュー「Windowsログイン時に起動」を
   一度外して付け直す。
4. 設定はコピーしかしていないので、旧 config.toml はそのまま使える。
   統合版側の設定(`%LOCALAPPDATA%\traytools\landrop\config.toml`)が
   残っていても、旧側は読みに行かないので無害。

## 注意

- 新旧を同時に起動しない(同じポートを取り合う)。片方を止めてから片方を起動。
- .lnk の名前は新旧で同じ(`LAN Drop.lnk`)。install で旧向き先は置き換わる
  ので、戻すときは上記3のとおり旧側で作り直す。
- うまく行かないときは `%LOCALAPPDATA%\traytools\landrop\` ではなく
  tray-tools 直下の `landrop.log` にログが出る(既定設定のとき)。
