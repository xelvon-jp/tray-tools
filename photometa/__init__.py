"""写真の★レーティングとキーワードを扱う共通パッケージ（Qt に依存しない）。

あっふ（tray-tools）と PhotoCuller の両方から読み込む。

- xmp         … ファイル先頭の XMP を自前で読む（速い。ExifTool を使わない）
- config      … 共通設定 %LOCALAPPDATA%\\traytools\\photo_ratings.json と写真メニューの中身
- writer      … ExifTool を常駐させて★・キーワードを書く。変更記録（journal）も
- filter_expr … 表示フィルタの条件式（人物名・★・キーワード・選択中など）
"""
