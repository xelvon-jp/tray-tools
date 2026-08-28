# settings.py
# settings.json の読み書きを行う。存在しない場合は自動生成し、壊れている場合は
# デフォルトにフォールバックする(rapture-py/settings.py の挙動を踏襲)。
import json
import time
from pathlib import Path

# 既定の保存先。ドライブ直下(C:\bak 等)は権限の無いPCでは作成に失敗しうるので、
# 必ず書けるユーザープロファイル配下にする。
DEFAULT_SAVE_FOLDER = str(Path.home() / "Pictures" / "rapture")

# 音声デバイスIDはPCごとに異なるGUIDなので、既定値としては持てない(他PCで使うと
# 存在しないIDになる)。setup.py での検出か list_devices.py の出力から設定する。
DEFAULT_SETTINGS = {
    "capture": {
        "hide_duration_ms": 2000,
        "save_folder": DEFAULT_SAVE_FOLDER,
        "save_format": "png",
        "jpeg_quality": 90,
        "history_days": 0,
        "pen_color": "#ff0000",
        "pen_width": 3,
        "highlighter_enabled": False,
        # 付箋が出るまでの待ちを消すための待機プロセス。有効(既定)にすると、Python と Qt を
        # 読み込んで最初の描画まで済ませた付箋プロセスを常に1つ起こしておき、キャプチャの
        # ときはそこへ画像と表示位置を送るだけにする(範囲を選んでから付箋が出るまで
        # 実測10ms前後)。false にすると1枚ごとにプロセスを起こす従来の形へ戻る
        # (このPCで355ms、遅いPCでは3秒以上かかるという報告もある)。
        # 代償は常時1プロセスぶんのメモリ(専有27MB程度)だけ。有効でも無効でも、
        # キャプチャそのものの成否は変わらない(待機役が居なければ従来の道へ落ちる)。
        "prewarm": True,
    },
    "screen": {
        # スリープ抑止サブメニューに並べる時限(分)。「無期限」と「解除」は常に付く。
        "keep_awake_minutes": [30, 120],
        # マウスジグラーも同じ作り(時限の選択肢＋無期限＋解除)。スリープ抑止と違って
        # 周期的に入力を送るので、送る間隔と「何秒無操作なら送るか」も持つ。
        # 間隔の既定60秒は、リモートデスクトップのアイドルタイムアウト(通常10〜15分)に
        # 対して十分余裕がある値。
        "jiggler_minutes": [30, 120],
        "jiggler_interval_seconds": 60,
        "jiggler_idle_seconds": 30,
    },
    "audio": {
        "devices": [],
    },
    # 外から呼ぶツール。空なら同梱のものを使う(presenter.html はこのフォルダにある)。
    # 別の場所に置いたものを使いたいときだけパスを書く。
    #
    # web_presenter_* は「🌐 サイトを取り込んで開く」(web_presenter.py)の調整用。
    #   timeout_seconds … 読み込みの打ち切り。loadFinished が永遠に来ないサイトがある
    #     ので必ず切る。重いページを相手にして毎回間に合わないなら伸ばす。
    #   settle_ms … 読み込み完了から DOM を取り出すまでの追加の待ち。load 直後に中身を
    #     書き足すページ向け。骨組みだけしか取り込めないときは伸ばす。
    #   strip_scripts … 取り込んだ HTML から <script> を落とす。取り込んだ JS が
    #     発表者ツールの iframe の中で暴れて表示が崩れるサイト向けの逃げ道。
    #     既定は false(スライド送りを JS が担う資料もあるため)。
    #   web_presenter_recent … 入力欄に出す履歴。取り込みに成功したときだけ増える。
    "tools": {
        # HTMLを開くのに使うブラウザ。edge / chrome / firefox、または実行ファイルの
        # フルパス。既定のブラウザに任せないのは、Firefox だと presenter.html が
        # 真っ白になるため(about:blank へ document.write する作りと、Firefox の
        # file:// の扱いが噛み合わない。Chrome と Edge では動く)。
        "browser": "edge",
        "presenter": "",
        "web_presenter_timeout_seconds": 30,
        "web_presenter_settle_ms": 800,
        "web_presenter_strip_scripts": False,
        "web_presenter_recent": [],
    },
    # 画面に重ねて使うプレゼン支援(presenter_overlay.py)。レーザーポインタ・
    # スポットライト・黒画面/白画面。presenter.html がブラウザの中だけの道具なのに対し、
    # こちらは画面の上に重ねるので対象を選ばない(ウェブでも PowerPoint でも PDF でも効く)。
    #
    # poll_interval_ms は、透過した窓では受け取れないカーソル位置を QCursor.pos() で
    # 読みに行く周期。16で60fps相当。滑らかさと負荷の兼ね合いは presenter_overlay.py の
    # DEFAULT_POLL_INTERVAL_MS に実測値付きで書いてある(33にすると負荷は半分)。
    # partial_repaint は「動いた周りだけ描き直す」。万一描き残し(尾を引く)が出たら
    # false にすると毎回全面を描き直す。
    #
    # target_screen は出す先。"cursor"(既定)ならカーソルのある画面1枚、"all" なら全画面。
    # 既定を1枚にしてあるのは、マルチディスプレイで全画面を覆うと手元の資料や発表者
    # ツールまで見えなくなるため。レーザーとスポットライトは、カーソルが別の画面へ
    # 移れば窓ごと追いかける(黒画面/白画面は出した画面に留まる)。
    #
    # laser_radius は光点の芯の半径、laser_glow_radius はその外に広がる淡い光の半径(px)。
    # spotlight_radius は素通しの半径、spotlight_feather はその外側で減光へ戻すまでの幅、
    # spotlight_dim は周囲の暗さ(0〜1、1で真っ黒)。
    # spotlight_radius と spotlight_dim は、画面ミラー中なら手元のツールバーの上で
    # ホイール(暗さは Shift+ホイール)を回して発表の最中に変えられる。変えた値はここへ
    # 書き戻されるので、次に始めるときも同じ設定で始まる。
    # blank_click_to_close を false にすると、黒画面/白画面はクリックでは消えなくなる
    # (ホットキーとトレイメニューだけで解除する)。
    "presenter_overlay": {
        "poll_interval_ms": 16,
        "partial_repaint": True,
        "target_screen": "cursor",
        "laser_radius": 9,
        "laser_glow_radius": 26,
        "laser_color": "#ff2d2d",
        "laser_opacity": 0.9,
        "spotlight_radius": 140,
        "spotlight_feather": 60,
        "spotlight_dim": 0.72,
        "blank_black_color": "#000000",
        "blank_white_color": "#ffffff",
        "blank_click_to_close": True,
    },
    # 手元の画面の一部を別のモニタへ全画面でミラーする「画面ミラー」(screen_mirror.py)。
    # 範囲を選んだあともその中は普通に操作でき、操作した結果がそのまま向こうに映る。
    # 画面共有(Teams等)に出しているモニタへ向けて使う。
    #
    # 選ぶ範囲の大きさは自由で、固定されるのは縦横比だけ。そのぶん大きく選ぶほど重い。
    # このPCでの実測は 1280x720 で 29.6fps(1コアの71%)、1920x1080 で 25.8fps(108%、
    # コマ落ちする)。重いと感じたら範囲を小さくするか、fps を落とす。範囲選択中は
    # その大きさで出せそうなfpsが出るし、ミラー中は実測値が手元の枠に出る。
    #
    # fps は1秒あたりに送る枚数(1〜60)。既定30。
    # target_screen_name は出す先のモニタ名(QScreen.name())。トレイメニューの
    #   「ミラー先」から選ぶとここへ書かれる。空なら「選択範囲が乗っていないモニタ」の
    #   先頭を自動で使う(モニタが3枚以上ある構成では明示的に選ぶこと)。
    # aspect は範囲選択の既定の縦横比("16:9" / "4:3" / "free")。選択中も 1/2/3 キーと
    #   ホイールで切り替えられる(切り替えた結果はその起動の間だけ覚える)。
    # cursor_* はミラー先に描く矢印。実カーソルは映らないので自前で描いている。
    #   拡大率によらず一定の大きさにしてあり、cursor_size はその一辺(論理px)。
    # click_ripple はクリックした瞬間にミラー先へ波紋を出す。画面共有では「今押した」が
    #   伝わりにくいため。_ms は消えるまでの時間、_radius は広がりきったときの半径。
    # source_frame は手元に「いまミラーしている範囲」の枠を出す。枠は範囲の外側に描くので
    #   ミラーには映り込まない。枠の外の帯に実測fpsも出る。
    # toolbar は手元の枠の下に出す操作パネル(レーザー・スポットライト・黒画面・白画面・
    #   カンペ・静止・範囲の選び直し・終了)。これも範囲の外に置くのでミラーには映り込まない
    #   (置ける場所が無いときは出ない)。右側の説明欄はタイトルバーを兼ねていて、そこを
    #   ドラッグするとミラー範囲ごと動く(大きさは変わらない)。アイコンの上のドラッグでは
    #   動かない——押すつもりで滑っただけで範囲が動いては困るため。
    #   ツールバーの上でホイールを回すとスポットライトの半径、Shift(かCtrl)+ホイールで
    #   暗さが変わる(スポットライトのアイコンの上なら消えていても効く)。変えた値は
    #   presenter_overlay の spotlight_radius / spotlight_dim へ書き戻される。
    # notes は手元のカンペ(発表者だけが見るメモ)を範囲の右隣に出す。中身は notes/
    #   フォルダに置いた *.md / *.txt で、ファイル名がそのまま表示名になる(定型文の
    #   snippets/ と同じ流儀)。読むだけのパネルで、書き足すのは「編集」ボタンから
    #   外部エディタ、直したら「再読込」。「# 見出し」の章は ◀ ▶ でワンクリックで
    #   行き来でき、「目次」で一覧(## は字下げして一緒に並ぶ)、「一覧」で別のカンペへ
    #   切り替えられる。文字の大きさは A- / A+ と Ctrl+ホイール。
    #   置き場所は範囲の右、入らなければ左、どちらも無理なら出さない——共有側に
    #   映してよいものではないので、撮影範囲に掛けるくらいなら出さない。
    # notes_width はそのパネルの幅(論理px)。240 を下回る指定は 240 に丸める
    #   (折り返しだらけで読めないものを出しても意味が無い)。
    # notes_font_size は本文の大きさ(7〜32)。発表中に A- / A+ や Ctrl+ホイールで
    #   変えると、ここへ書き戻される。
    # notes_file は最後に開いていたカンペの表示名。次に始めたときも同じものが開く
    #   (消されていたら先頭のものへ寄せる)。
    # freeze_frame_* は静止(一時停止)しているときの枠の色。通常の枠より目立たせてある。
    #   静止したまま話し続けるのが最悪なので、ここは主張してよい。
    # blank_frame_* はミラー先だけを黒画面/白画面で覆っているときの枠の色。ミラー中の
    #   黒画面/白画面は向こうだけを覆い、手元は普通に見えたままにする(次に何を見せるか
    #   準備できるように)。そのぶん手元の見た目でしか気付けないので、静止とは別の色で
    #   主張する。ミラーしていないときの黒画面/白画面は従来どおりカーソルのある画面を
    #   覆う(presenter_overlay の blank_* を使う)。
    #
    # scaling は拡大方法。"auto"(既定) / "smooth" / "fast"。拡大する以上、補間による
    #   滲みは避けられない(Qt の選択肢は双線形と最近傍の2つだけで、間は無い)。本当の解は
    #   「等倍で映すこと」で、そのために presets に「ミラー先と同じ解像度」を入れてある。
    #   auto は倍率が整数のときだけ fast(最近傍)にする。整数倍なら元の1画素が正方形へ
    #   そのまま分かれるので中間色が生えず、輪郭が鈍らない。半端な倍率で fast にすると
    #   行ごとに太さの違う文字になるので smooth へ落とす。
    #
    # presets は範囲選択中に一発で選べる矩形。一覧が画面に出て、クリックか Ctrl+数字で
    #   選べる。書ける値は
    #     label  … 一覧に出す見出し
    #     width / height … 大きさ(論理px)
    #     size: "target"  … 大きさをミラー先のモニタと同じにする(＝等倍。滲まない)
    #     x / y  … 始点。省略するとその画面の中央。既定では「今カーソルのあるモニタの
    #              左上」からの相対で、screen に QScreen.name() を書けばそのモニタに固定
    #              できる。相対なのは、(0,100) のような値が「作業しているモニタの
    #              タスクバーやアドレスバーを外した位置」の意味で書かれるため。
    #
    # レーザーとスポットライトの色や大きさはここには無い。ミラー先へ描くときも
    # presenter_overlay セクションの値をそのまま使う(同じ道具の同じ光点なので)。
    "screen_mirror": {
        "fps": 30,
        "target_screen_name": "",
        "aspect": "16:9",
        "scaling": "auto",
        # 先頭が「範囲を選ばずに開始したとき」の既定になる。画面の左上に寄せ、縦だけ
        # タイトルバーのぶん下げてある。ブラウザやアプリの枠を外して中身から映すため。
        "presets": [
            {"label": "FHD（左上）", "x": 0, "y": "titlebar", "width": 1920, "height": 1080},
            {"label": "等倍（ミラー先と同じ）", "size": "target"},
            {"label": "上を100空ける", "x": 0, "y": 100, "width": 1600, "height": 900},
            {"label": "HD", "width": 1280, "height": 720},
        ],
        "cursor_size": 34,
        "cursor_color": "#ffffff",
        "cursor_outline": "#101010",
        "click_ripple": True,
        "click_ripple_ms": 420,
        "click_ripple_radius": 62,
        "click_ripple_color": "#ffd400",
        "source_frame": True,
        "source_frame_color": "#00c8ff",
        "source_frame_width": 3,
        "source_frame_opacity": 0.55,
        "freeze_frame_color": "#ff8c00",
        "freeze_frame_opacity": 0.9,
        "blank_frame_color": "#a855f7",
        "blank_frame_opacity": 0.9,
        "toolbar": True,
        "notes": True,
        "notes_width": 380,
        "notes_font_size": 11,
        "notes_file": "",
    },
    # フォルダブックマーク。bookmarks は {"name": 表示名, "path": フォルダパス} の配列で、
    # アプリからの登録で増える(削除・並べ替えは settings.json を直接編集する想定)。
    # target は移動先の決め方: auto(呼んだ時の前面がエクスプローラならそこ、でなければ
    # あふｗ) / afxw(常にあふｗ) / explorer(常にエクスプローラ)。
    "launcher": {
        "target": "auto",
        "afxw_path": r"C:\soft\afxw\AFXW.EXE",
        "bookmarks": [],
    },
    # 定型文。recent は最近コピーしたテンプレート名(新しいものが先頭・上限20件)で、
    # ピッカーの並び順に使う。アプリが自分で書き足す値なので手で編集する必要はない。
    "snippets": {
        "recent": [],
    },
    # 各ディスプレイのタスクバーに置く、通知領域の代わりの小さなウィジェット。
    # Windowsは通知領域をプライマリのタスクバーにしか出さないため、正面のモニタを
    # セカンダリにしている構成では、トレイを触るたびに視線と手が別の画面へ行ってしまう。
    # 既定は無効。タスクバーが1つしか無いPC(そのまま設定を持ち回るノート等)で勝手に出さない。
    #
    # all_displays が True ならプライマリを含む全ディスプレイに1つずつ出す。False なら
    # セカンダリだけ。置き先はタスクバーの数ではなく画面の数で決めるので、Windowsの
    # 「タスクバーをすべてのディスプレイに表示する」がオフの環境でも全画面に出る
    # (その画面ではタスクバーがあるはずの位置＝画面下端を基準にする)。
    #
    # positions は位置をディスプレイごとに持つ辞書。キーは QScreen.name()(Windowsなら
    # "\\.\DISPLAY1" 等)、値は {"right": 右端からの距離, "top": 基準の上端からの距離}。
    # Ctrl+左ドラッグで動かしたウィジェットの分だけがここへ書かれる(他の画面は動かない)。
    # 書かれていない画面は既定位置(Windows 11 の時計の実測位置)へ自動配置するので、
    # 別のPCへこのファイルを持って行っても壊れない(知らない画面名は既定に落ちるだけ)。
    #
    # background_color が None なら、表示する直前にその位置の画面を撮って最頻色を使う
    # (タスクバーの透明効果で壁紙が透けるため、決め打ちの色では浮く)。
    # clock_format_* は strftime の書式。先頭ゼロを落とすのは Windows では %#H(%-H はLinux系)。
    #
    # launcher_* は、ウィジェットにマウスを乗せている間だけ真上に出る縦一列のランチャ
    # (taskbar_launcher.py)。本体は時計に化ける都合で幅が59px前後しかなく、Rapture と
    # 音声の2つで埋まる。それ以外の機能へ通知領域まで戻らずに届くようにするためのもの。
    # launcher_items は上から下へ並ぶ順で、書ける名前は
    # capture(キャプチャ) / audio(音声出力切替) / ruler(画面定規) /
    # color_picker(カラーピッカー) / snippets(定型文) / bookmarks(フォルダブックマーク) /
    # presenter(発表者ツール) / laser(レーザーポインタ) / spotlight(スポットライト) /
    # blackout(黒画面) / whiteout(白画面) / screen_mirror(画面ミラー)。
    # 減らしても増やしても順を入れ替えてもよい。[] にするか launcher_enabled を False に
    # すると、マウスを乗せても従来どおり本体だけになる。
    # launcher_close_delay_ms は、カーソルが離れてから畳むまでの猶予。本体からパネルへ
    # 移る途中には必ず「どちらにも乗っていない」瞬間があるので、0にすると項目へ届く前に
    # 閉じる。
    "taskbar_widget": {
        "enabled": False,
        "all_displays": True,
        "positions": {},
        "width": None,
        "height": 31,
        "background_color": None,
        "text_color": None,
        "clock_format_top": "%m/%d(%a)",
        "clock_format_bottom": "%H:%M:%S",
        "launcher_enabled": True,
        "launcher_items": [
            "capture",
            "audio",
            "ruler",
            "color_picker",
            "snippets",
            "bookmarks",
            "presenter",
        ],
        "launcher_item_size": 36,
        "launcher_close_delay_ms": 300,
    },
    # 空文字にすると、そのホットキーは登録されない(無効化できる)。
    "hotkeys": {
        "audio_toggle": "ctrl+alt+h",
        "capture_now": "ctrl+alt+r",
        "capture_sequence": "ctrl+alt+s",
        "mic_mute": "ctrl+alt+m",
        "color_picker": "ctrl+alt+c",
        "always_on_top": "ctrl+alt+t",
        "snippet_picker": "ctrl+alt+v",
        # あふｗ側でも同じ機能を J に割り当てているので、単独で呼ぶときも同じ指に置く。
        "launcher": "win+j",
        # 画面に重ねるプレゼン支援(presenter_overlay.py)。レーザーとスポットライトは
        # マウスを透過する＝自分ではキーもマウスも受け取れないので、ここが唯一の
        # 「畳む手段」になる(トレイメニューからも切れるが、発表中に通知領域まで
        # 手を伸ばすことは考えにくい)。空文字にするとその機能が閉じられなくなるため、
        # 消すならメニューだけで足りるか考えてから。
        # 文字は l=laser / o=spOtlight / b=black / w=white。既に使っている
        # h,r,s,m,c,t,v と win+j のどれとも重ならない組み合わせを選んである。
        "presenter_laser": "ctrl+alt+l",
        "presenter_spotlight": "ctrl+alt+o",
        "presenter_blackout": "ctrl+alt+b",
        "presenter_whiteout": "ctrl+alt+w",
        # 画面ミラー(screen_mirror.py)の開始と「範囲の選び直し」。押すと範囲選択が
        # 出て、選ぶとミラーが始まる。ミラー中に押すと、映したまま範囲だけを選び直す
        # (発表の途中で映す場所を変えたいときに、いったん共有が途切れないように)。
        # p は presentation の p。
        #
        # このキーでは終われないので、終了は screen_mirror_stop に持たせてある
        # (トレイメニューの「⏹ 終了」と手元のツールバーの ✕ でも終われる)。
        # q は quit の q、f は freeze の f。どちらも既に使っている
        # h,r,s,m,c,t,v,l,o,b,w,p と win+j のどれとも重ならない。
        #
        # ミラー中のレーザーとスポットライトに別のキーは要らない。上の
        # presenter_laser / presenter_spotlight がそのままミラー先の光点になる
        # (手元に重ねてしまうと、それが撮られて向こうへ二重に映る)。
        "screen_mirror": "ctrl+alt+p",
        "screen_mirror_stop": "ctrl+alt+q",
        # 静止(一時停止)。押すと今の1枚で止まり、もう一度押すと現在の画面へ戻る。
        # 手元で資料を切り替える間、その様子を見せないためのもの。
        "screen_mirror_freeze": "ctrl+alt+f",
    },
}

# settings.py 自身の置き場所を基準にする。cwd に依存させない。
SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"


def _deep_merge(base: dict, override: dict) -> dict:
    """base(デフォルト)にoverride(読み込んだ値)を再帰的に重ねる。
    セクションごと上書きすると未指定キーが消えてしまうため、辞書同士は再帰的にマージする。"""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings(path=None) -> dict:
    """settings.json を読み込む。存在しない/壊れている場合はデフォルトにフォールバックする。"""
    target_path = Path(path) if path else SETTINGS_PATH
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # ディープコピー

    if target_path.exists():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                merged = _deep_merge(merged, loaded)
        except (json.JSONDecodeError, OSError):
            # 壊れたJSONはそのまま使わず、デフォルト値で継続する
            pass
    else:
        save_settings(merged, target_path)

    return merged


def save_settings(settings_dict: dict, path=None) -> None:
    """settings.json へ書き込む。保存先フォルダが無ければ作成する。"""
    target_path = Path(path) if path else SETTINGS_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(settings_dict, f, ensure_ascii=False, indent=2)


def cleanup_old_captures(capture_settings: dict) -> None:
    """history_days > 0 のとき、保存フォルダ内のN日以上前のキャプチャファイルを削除する。
    デフォルト(0)では何もしない=自動削除しない。
    保存形式を変えても古いファイルが取り残されないよう rapture_*.* を対象にする。"""
    history_days = capture_settings.get("history_days", 0)
    if not history_days or history_days <= 0:
        return

    save_folder = Path(capture_settings.get("save_folder", DEFAULT_SETTINGS["capture"]["save_folder"]))
    if not save_folder.exists():
        return

    cutoff = time.time() - (history_days * 86400)

    for file_path in save_folder.glob("rapture_*.*"):
        try:
            if file_path.stat().st_mtime < cutoff:
                file_path.unlink()
        except OSError:
            # 使用中などで削除できないファイルはスキップする
            pass
