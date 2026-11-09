# LAN Drop(tray-tools 統合版)

スマホ(iPhone)から PC へ、LAN 内でファイルとテキストを受ける常駐ツール。
tray-tools の一部として取り込まれているが、プロセスは独立
(`pythonw -m landrop --tray`)で、専用トレイアイコンを持つ(案B)。
受信の仕組み・iPhone 側の使い方は単体時代と変わらない。

## セットアップ(受信に使う PC だけでよい)

1. tray-tools の通常のセットアップを済ませる。
2. LAN Drop 専用の依存を venv に入れる(本体の requirements とは分けてある)。

   ```powershell
   & "C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe" -m pip install -r requirements-landrop.txt
   ```

3. スタートアップ登録をする(ログイン時に自動で立ち上がる)。

   ```powershell
   & "C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe" -m landrop.startup_link install
   ```

   単体時代から移る場合は、旧設定(トークン・保存先)を引き継ぐ:

   ```powershell
   & "C:\Users\yotan\.venvs\tray-tools\Scripts\python.exe" -m landrop.startup_link install --from-config "C:\Users\yotan\Desktop\claude\lan-drop\config.toml"
   ```

   登録の現状確認は `python -m landrop.startup_link status`、
   登録を消すときは `python -m landrop.startup_link remove`。
   トレイのメニュー「Windowsログイン時に起動」でも付け外しできる。

4. すぐ使うときは手で起動する(次回ログインからは自動)。

   ```powershell
   Start-Process "C:\Users\yotan\.venvs\tray-tools\Scripts\pythonw.exe" -ArgumentList "-m","landrop","--tray" -WorkingDirectory "R:\claude\tray-tools"
   ```

## 設定

- 置き場: `%LOCALAPPDATA%\traytools\landrop\config.toml`
  (初回起動時にトークン入りで自動生成される)。
- 単体時代の設定が tray-tools 直下に残っている場合は、そちらが優先で
  読み込まれる(自動では移動しない)。
- 保存先・ポート・通知の有無などは config.toml とトレイのメニューで変更。
- iPhone 側は、トレイのメニューから案内ページ(QR)を開いて設定する(従来どおり)。

## 切り替え手順(実機での本番切り替え)

旧い単体 LAN Drop から統合版へ移す手順と戻し方は
`docs/landrop-switch.md` にある。
