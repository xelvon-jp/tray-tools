---
id: 20261105-04-copilot-review-fixes
status: done
base_sha: 58a1eef19d49ece8629f381429ff6c9899a888e9
head_sha: 58a1eef19d49ece8629f381429ff6c9899a888e9
branch: muse/20261105-04-copilot-review-fixes
approvals: 1（内訳: push 1。ブランチは作成済みのため作成 0、読み取りはカードなし）
started: 2026-11-05 19:25 JST
finished: 2026-11-05 19:28 JST
---

## 結果

レビュー指摘3点をすべて直した。変更ファイルは下の7件（README.md は確認のうえ変更なし:
現行の「`copilot_status.enabled` は旧仕様の名残りで、もう書きません（`bar` が無い PC での
初期値としてだけ読みます）」という説明は、修正後の挙動とも一致するため）。

1. **旧設定 `copilot_status.enabled` の引き継ぎ**
   - `copilot_watchdog.py` に共通判定を新設: `bar_enabled_from_section(section)`
     （`bar` があればそれ → 無く `enabled` があればそれ → どちらも無ければ `True`）、
     `read_raw_copilot_section(settings_path)`（settings.json を直接読み、マージ前の
     `copilot_status` 節を返す。読めなければ `None`）、`resolve_bar_enabled(...)`。
     このモジュールは UIA / comtypes を読まないので、常駐からも子からも安全。
   - 親 `CopilotWatchdog.__init__` は `settings_path` があれば生ファイルで判定
     （パスが無いときだけ渡された節で判定）。子 `StatusWatcher` の初期化と
     `_refresh_bar_setting` も同じ共通関数・生ファイルに切り替えた
     （読み直しに失敗したときは子の前の値を保つ）。
   - `settings.py` の `DEFAULT_SETTINGS["copilot_status"]` から `bar` / `enabled` を
     外し（節自体は空で残す）、コメントを「既定値をここに置くと深いマージで `bar` が
     常に入り、引き継ぎが働かなくなるため置かない」に書き換えた。
   - `set_bar_enabled` / `_save_section` の「該当キーだけ差し替え」は不変。
2. **`_activate_copilot` の ctypes 型指定**
   - `feature_screen.py`: `ctypes.windll.user32` への直接指定をやめ、呼ばれたときに
     だけ専用の `ctypes.WinDLL("user32", use_last_error=True)` を作り、`IsWindow` /
     `IsIconic` / `ShowWindow` / `SetForegroundWindow` に `HWND`（`wintypes.HWND`）
     引数と `BOOL` 戻り値（`ShowWindow` は `(HWND, int)`）を指定。hwnd は
     `wintypes.HWND(hwnd)` で渡す。import 時には作らないので Linux でも
     feature_screen の import は落ちない（静的確認済み）。
3. **`.gitignore`**
   - `copilot_loop_status.json` の近くに、同じ書き方（1行コメント＋パス）で
     `copilot_turn_status.json` と `copilot_turn_status.json.tmp` を追加。

テスト（`tests/test_copilot_status.py`）:
- 追加: `test_bar_enabled_from_section_cases`（節なし→True／`{"enabled": false}`だけ
  →False／`{"enabled": false, "bar": true}`→True 等）、
  `test_bar_enabled_from_raw_file_and_child_agrees`（生ファイル4ケースで
  `resolve_bar_enabled` と親 `CopilotWatchdog` が一致し、子
  `StatusWatcher._refresh_bar_setting` も同じ答え。子のモジュールは
  `ctypes.windll` 前提で Linux では import できないため、子の確認は import できた
  環境でのみ実行するよう条件付きにした）。
- 変更: `test_settings_defaults_have_copilot_status` は「既定に `bar` / `enabled` が
  無く、読み込んだ節は `{}`」の確認に更新。
- 実行結果（Linux VM、PySide6 offscreen 環境）:
  - 通常実行: **11/13 passed**。残り2件（`test_publish_turn_writes_on_change_and_heartbeats`、
    `test_classify_delete_all_decays_to_waiting_user`）は VM に `mss` / `comtypes` が
    無く `copilot_status_process` を import できないための失敗で、今回の変更前から
    同じ理由で落ちる環境要因（変更とは無関係）。
  - Windows 専用依存（`ctypes.windll`・`capture_grab`・`copilot_loop`）をスタブに
    差し替えた実行では **13/13 passed**。同じスタブ環境で子の実物
    `StatusWatcher` の生成（初期化の判定）も4ケース全て親と一致することを確認した。
  - 変更4モジュール＋テストの `py_compile` 通過。

触らないこと（依頼どおり未変更）: 進捗パネルの Copilot 行の前面化そのもの、
`WAITING_AI_DECAY_SECONDS = 6`。所見の追加はなし（前面化については型指定の修正のみで、
前面化ロックの有無は実機でしか判断できないため）。

## 未実施・実機確認待ち

- Windows 実機での `tests/test_copilot_status.py` の通常実行（venv の python.exe）。
- 旧仕様で `enabled: false` にしていた PC（または `copilot_status` に `enabled` だけが
  ある settings.json）で、更新後にバーが出ないことの実地確認。
- 進捗パネルの Copilot 行クリックで Copilot が実際に前面化すること（最小化からの
  復元含む）。型指定なしで例外になっていたケースが直ったかも含め実機でしか
  確かめられない。
- 常駐の再起動を伴う一連の動作（バー表示の切替が子の再起動なしで反映されること）。

## base と現在の main の差

なし。着手時点で main の HEAD は base_sha（58a1eef）そのもので、対象ファイルの
内容は base と同一だった（`.gitignore` の blob SHA で代表確認済み）。

## 連携についてのメモ

- 依頼書の frontmatter の `base_sha` が「<依頼の1行に書く>」のままだったが、依頼の
  1行に明記されていたので問題なく着手できた。次回もこの形でよい。
- 依頼書の「テストに足す」4ケースは、そのままテスト名・ケースとして実装できた。
  1点だけ、子のモジュールが Linux で import できない（`ctypes.windll` 前提）ため、
  子の一致確認は Windows でだけ実体が走る条件付きにした。Linux 側ではスタブ差し替えで
  実物の初期化・読み直しまで確認済みなので、Windows 実機では通常実行でそのまま通る見込み。
