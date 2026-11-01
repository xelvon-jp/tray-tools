"""写真の★レーティングとキーワードを扱う共通パッケージ（Qt に依存しない）。

あっふ（tray-tools）と PhotoCuller の両方から読み込む。

- xmp         … ファイル先頭の XMP を自前で読む（速い。ExifTool を使わない）
- config      … 共通設定 %LOCALAPPDATA%\\traytools\\photo_ratings.json と写真メニューの中身、★の表示名
                （rating_name / rating_tag / rating_short / range_label）
- writer      … ExifTool を常駐させて★・キーワードを書く。変更記録（journal）も
- metacache   … ★の読み取り結果の永続キャッシュ（SQLite。stat が一致すれば開かずに返す）
- filter_expr … 表示フィルタの条件式（人物名・★・キーワード・選択中など）
- export      … 書き出し（フォトフレームへの同期・みてね用の収集と片付け）
"""
