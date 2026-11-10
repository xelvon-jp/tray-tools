# chara_sprites.py
# 進捗パネルの「連携ステージ」に出す、ドット絵キャラクター(Muse と Claw'd)の
# 絵データと、どのフレームをいつ出すかの純関数。Qt に依存しない(描くのは claude_progress)。
#
# 絵は実行時に外のファイルを読まず、ここに文字の格子として埋め込む。
# Muse のキャラは近く差し替える予定なので、Muse の絵は MUSE_PALETTE / MUSE_FRAMES の
# 1か所にまとめてある(差し替えるときはここだけ)。
# 動き(周期・判定)は動くデモ(anim_template.html)の frameFor / museFrame と同じ。

# ---- Claw'd(12x8)。デモの claw() と同じ作り -------------------------------------
CLAWD_PALETTE = {
    "O": "#DA7758",   # 体
    "e": "#1a1a1a",   # 目
    "d": "#9c4a2a",   # 閉じ目
    "P": "#B088FF",   # 帽子
    "s": "#7D6AB8",   # 帽子の軸
}


def _claw(al=False, ar=False, legs="n", eyes=None) -> list:
    g = [["."] * 12 for _ in range(8)]
    for y in range(6):
        for x in range(2, 10):
            g[y][x] = "O"

    def arm(x0, up):
        y0 = 1 if up else 2
        for y in range(y0, y0 + 2):
            for x in range(x0, x0 + 2):
                g[y][x] = "O"

    arm(0, al)
    arm(10, ar)
    leg_set = {"n": [(2, 2), (4, 2), (7, 2), (9, 2)],
               "A": [(2, 2), (4, 1), (7, 2), (9, 1)],
               "B": [(2, 1), (4, 2), (7, 1), (9, 2)]}[legs]
    for x, h in leg_set:
        for y in range(h):
            g[6 + y][x] = "O"
    for x, y, c in (eyes if eyes is not None else [(3, 1, "e"), (8, 1, "e")]):
        g[y][x] = c
    return ["".join(r) for r in g]


CLAWD_FRAMES = {
    "base": _claw(),
    "blink": _claw(eyes=[(3, 1, "d"), (8, 1, "d")]),
    "lookL": _claw(eyes=[(2, 1, "e"), (7, 1, "e")]),
    "lookR": _claw(eyes=[(4, 1, "e"), (9, 1, "e")]),
    "walkA": _claw(legs="A"),
    "walkB": _claw(legs="B"),
    "waveA": _claw(ar=True, eyes=[(2, 1, "e"), (7, 1, "e")]),
    "waveB": _claw(eyes=[(2, 1, "e"), (7, 1, "e")]),
    "alert": _claw(eyes=[(3, 1, "e"), (3, 2, "e"), (8, 1, "e"), (8, 2, "e")]),
    "happy": _claw(al=True, ar=True,
                   eyes=[(3, 1, "d"), (8, 1, "d"), (2, 1, "d"), (9, 1, "d")]),
    "sleep": _claw(eyes=[(3, 1, "d"), (8, 1, "d")]),
}
# 帽子(12x6)。Claw'd の頭の真上に載せる。
HAT = [".....P......", "....PPP.....", ".....P......",
       ".....s......", "....PPPP....", "....PPPP...."]

# ---- Muse(v2。13列x15行。大きさはデータから取る) --------------------------------------
MUSE_PALETTE = {"B": "#E9D9B8", "F": "#FFF3D6", "E": "#222222", "P": "#F4A6B7", "S": "#7FC4FF"}
MUSE_FRAMES = {
    "idle": [
        ".....BBB.....",
        "...BBBBBBB...",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBFEFFFEFBB.",
        ".BBPFFEFFPBB.",
        ".BBFFFFFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "..BBB...BBB..",
    ],
    "blink": [
        ".....BBB.....",
        "...BBBBBBB...",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBFEEFEEFBB.",
        ".BBPFFEFFPBB.",
        ".BBFFFFFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "..BBB...BBB..",
    ],
    "work-a": [
        ".....BBB.....",
        "...BBBBBBB...",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBFFFFFFFBB.",
        ".BBFEFFFEFBB.",
        ".BBFFFEFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "..BBB...BBB..",
    ],
    "work-b": [
        ".....BBB.....",
        "...BBBBBBB...",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBFFFFFFFBB.",
        ".BBFEFFFEFBB.",
        ".BBFFFEFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        ".BBB.....BBB.",
    ],
    "wait": [
        ".....BBB.....",
        "...BBBBBBB...",
        "..BBBBBBBBB.S",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBFFEFFFEBB.",
        ".BBPFFFEFPBB.",
        ".BBFFFFFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "...BBB.BBB...",
    ],
    "alert": [
        ".....BBB.....",
        "S..BBBBBBB..S",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFEFFFEFBB.",
        ".BBFFFFFFFBB.",
        ".BBPFFFFFPBB.",
        ".BBFFFEFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "..BBB...BBB..",
    ],
    "happy": [
        ".S...BBB...S.",
        "...BBBBBBB...",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBFEEFEEFBB.",
        ".BBPFFFFFPBB.",
        ".BBFFFEFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "..BBB...BBB..",
    ],
    "sleep": [
        ".....BBB....S",
        "...BBBBBBB.S.",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBFFFFFFFBB.",
        ".BBFEEFEEFBB.",
        ".BBFFFFFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "..BBB...BBB..",
    ],
    "look-l": [
        ".....BBB.....",
        "...BBBBBBB...",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBEFFFEFFBB.",
        ".BBPFEFFFPBB.",
        ".BBFFFFFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "..BBB...BBB..",
    ],
    "look-r": [
        ".....BBB.....",
        "...BBBBBBB...",
        "..BBBBBBBBB..",
        ".BBBBBBBBBBB.",
        ".BBBFFFFFBBB.",
        ".BBFFFFFFFBB.",
        ".BBFFEFFFEBB.",
        ".BBPFFFEFPBB.",
        ".BBFFFFFFFBB.",
        ".BBBFFFFFBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        ".BBBBBBBBBBB.",
        "..BBBBBBBBB..",
        "..BBB...BBB..",
    ],
}


def muse_rows(name) -> list:
    """Muse のフレームの格子(知らない名前は idle)。"""
    return MUSE_FRAMES.get(name) or MUSE_FRAMES["idle"]


# ---- どのフレームをいつ出すか(デモの frameFor / museFrame と同じ周期・判定) --------
def clawd_frame(anim, t_ms) -> str:
    """Claw'd のフレーム名。anim は watch / work / wait / alert / happy / sleep。"""
    t = t_ms
    if anim == "watch":
        c = t % 4200
        if 3900 < c < 4060:
            return "blink"
        g = t % 6000
        if 1500 < g < 2300:
            return "lookL"
        if 3300 < g < 4100:
            return "lookR"
        return "base"
    if anim == "work":
        return "walkA" if int(t // 160) % 2 else "walkB"
    if anim == "wait":
        return "waveA" if int(t // 320) % 2 else "waveB"
    if anim in ("alert", "happy", "sleep"):
        return anim
    return "base"


def muse_frame(anim, t_ms) -> str:
    """Muse のフレーム名。anim は work / claude / approval / alert / done / sleep。"""
    t = t_ms
    if anim == "work":
        if t % 3000 < 500:
            return "look-r"
        return "work-b" if int(t // 240) % 2 else "work-a"
    if anim == "claude":
        c = t % 5200
        if 4900 < c < 5060:
            return "blink"
        g = t % 7000
        if 1800 < g < 2600:
            return "look-l"
        if 4200 < g < 5000:
            return "look-r"
        return "idle"
    if anim == "approval":
        return "wait" if int(t // 320) % 2 else "look-r"
    if anim == "alert":
        return "alert"
    if anim == "done":
        return "happy"
    if anim == "sleep":
        return "sleep"
    return "idle"


# ---- 体の動き(上下・揺れ・ジャンプ)。座標のオフセット(dx, dy)で返す ----------------
CLAWD_MOVE = {"watch": "bob", "work": "fast", "wait": "", "alert": "shake",
              "happy": "jump", "sleep": "sleep"}
MUSE_MOVE = {"work": "fast", "claude": "", "approval": "bob", "alert": "shake",
             "done": "jump", "sleep": "sleep"}


def move_offset(style, t_ms) -> tuple:
    """動きの種類(bob / fast / shake / jump / sleep / "")と時刻から (dx, dy) を返す。
    デモの CSS(.t-bob など。steps で段階的に動く)と同じ周期・振れ幅。"""
    t = t_ms
    if style == "bob":
        return (0, 0 if t % 1200 < 600 else -2)
    if style == "fast":
        return (0, 0 if t % 320 < 160 else -3)
    if style == "shake":
        return (-2 if t % 160 < 80 else 2, 0)
    if style == "jump":
        p = (t % 700) / 700.0
        if p < 0.3:
            k = int(p / 0.3 * 7) / 7.0
            return (0, -round(14 * k))
        if p < 0.6:
            return (0, -14)
        k = int((p - 0.6) / 0.4 * 7) / 7.0
        return (0, -round(14 * (1 - k)))
    if style == "sleep":
        return (0, 0 if t % 2400 < 1464 else 1)
    return (0, 0)


# ---- 紙吹雪(完了のとき)。決まった式で粒を出す(乱数の状態を持たない) -----------------
CONFETTI_COLORS = ["#ffd166", "#ff5a5f", "#4fe0ff", "#37e0b0", "#B088FF"]
CONFETTI_LIFE_MS = 1300
CONFETTI_SPAWN_MS = 180


def _rnd(n, j) -> float:
    """(n, j) から決まる 0〜1 の擬似乱数。"""
    v = (n * 2654435761 + j * 40503 + 12345) & 0xFFFFFFFF
    v ^= v >> 15
    v = (v * 2246822519) & 0xFFFFFFFF
    v ^= v >> 13
    return (v & 0xFFFF) / 65536.0


def confetti_particles(t_ms, stage_w) -> list:
    """いま見えている紙吹雪の粒。(x, y, 色, 回転角, 不透明度)。座標はステージの左上基準。
    180ms ごとに2粒、上から落ちて(1.3秒で78px)回りながら消える。"""
    out = []
    last = int(t_ms // CONFETTI_SPAWN_MS)
    for n in range(last - 8, last + 1):
        for j in range(2):
            delay = _rnd(n, 10 + j) * 200.0
            age = t_ms - n * CONFETTI_SPAWN_MS - delay
            if not 0 <= age < CONFETTI_LIFE_MS:
                continue
            prog = age / CONFETTI_LIFE_MS
            x = (stage_w - 124) + _rnd(n, j) * 120
            y = 6 + _rnd(n, 20 + j) * 10 + 78 * prog
            out.append((x, y, CONFETTI_COLORS[(n * 2 + j) % 5], 200 * prog, 1.0 - prog))
    return out


# ---- 連携ステージの場面(最終イメージ)の小物たち -------------------------------
# キャラデザインマトリクス(2026-11-08)の案A の格子をそのまま写したもの。
# 郵便受け=私書箱、見張り=フクロウ、配備係=ロボット、家=PC の印。
# 窓口のベルだけはマトリクスに無いので後付けの仮絵(2026-11-10)。
GITHUB_GRID = [
    '.........KK.........',
    '......KKKKKKKK......',
    '....KKKKKKKKKKKK....',
    '...KKWWKKKKKKWWKK...',
    '..KKKWWWWWWWWWWKKK..',
    '..KKKWWWWWWWWWWKKK..',
    '.KKKKWWWWWWWWWWKKKK.',
    '.KKKWWWWWWWWWWWWKKK.',
    '.KKKWWWWWWWWWWWWKKK.',
    'KKKKKWWWWWWWWWWKKKKK',
    'KKKKKWWWWWWWWWWKKKKK',
    '.KKWWWKWWWWWWKKKKKK.',
    '.KWWWWKWWWWWWKKKKKK.',
    '.KWWKKKWWWWWWKKKKKK.',
    '..WWWKKWWWWWWKKKKK..',
    '..KWWWWWWWWWWKKKKK..',
    '...KWWWWWWWWWKKKK...',
    '....KKKWWWWWWKKK....',
    '......KWWWWWWK......',
    '.........KK.........',
]
GITHUB_PALETTE = {'K': '#101012', 'W': '#FFFFFF'}

POST_GRID = [
    '............',
    '....RRRR..F.',
    '...RRRRRR.F.',
    '..RRRRRRRRF.',
    '..RRDDDDRR..',
    '..RRRRRRRR..',
    '..RRRRRRRR..',
    '..RRRRRRRR..',
    '...RRRRRR...',
    '.....GG.....',
    '.....GG.....',
    '....GGGG....',
]
POST_PALETTE = {'R': '#D64541', 'D': '#7E2721', 'F': '#FF7847', 'G': '#6B7280'}

WATCH_GRID = [
    'B..........B',
    'BB.BBBBBB.BB',
    '.BBBBBBBBBB.',
    '.BFFFFFFFFB.',
    '.BFFFFFFFFB.',
    '.BFEEFFEEFB.',
    '.BFEEFFEEFB.',
    '.BFFFFFFFFB.',
    '..BBBBBBBB..',
    '..BBBBBBBB..',
    '...BB..BB...',
]
WATCH_PALETTE = {'B': '#A9805A', 'F': '#E8DCC4', 'E': '#1A1A1A'}

WATCH_BLINK_GRID = [
    'B..........B',
    'BB.BBBBBB.BB',
    '.BBBBBBBBBB.',
    '.BFFFFFFFFB.',
    '.BFFFFFFFFB.',
    '.BFFFFFFFFB.',
    '.BFEEFFEEFB.',
    '.BFFFFFFFFB.',
    '..BBBBBBBB..',
    '..BBBBBBBB..',
    '...BB..BB...',
]

DEPLOY_GRID = [
    '.....A......',
    '.....M......',
    '..MMMMMMMM..',
    '..MMMMMMMM..',
    '..MeeMMeeM..',
    '..MMMMMMMM..',
    '.MMMMMMMMMM.',
    '..MMMMMMMM..',
    '...MM..MM...',
    '...MM..MM...',
]
DEPLOY_PALETTE = {'M': '#3FA7A0', 'e': '#BFF3EC', 'A': '#FFD166'}

DEPLOY_WALK_A = [
    '.....A......',
    '.....M......',
    '..MMMMMMMM..',
    '..MMMMMMMM..',
    '..MeeMMeeM..',
    '..MMMMMMMM..',
    '.MMMMMMMMMM.',
    '..MMMMMMMM..',
    '...MM..MM...',
    '..MM....MM..',
]

DEPLOY_WALK_B = [
    '.....A......',
    '.....M......',
    '..MMMMMMMM..',
    '..MMMMMMMM..',
    '..MeeMMeeM..',
    '..MMMMMMMM..',
    '.MMMMMMMMMM.',
    '..MMMMMMMM..',
    '..MM....MM..',
    '...MM..MM...',
]

HOME_GRID = [
    '.....RR.....',
    '....RRRR....',
    '...RRRRRR...',
    '..RRRRRRRR..',
    '.RRRRRRRRRR.',
    '..WWWWWWWW..',
    '..WYYWWDDW..',
    '..WYYWWDDW..',
    '..WWWWWDDW..',
    '..WWWWWWWW..',
    '............',
]
HOME_PALETTE = {'R': '#C0533B', 'W': '#E9DFC9', 'Y': '#FFD166', 'D': '#8A5A33'}

PC_GRID = [
    '..........................',
    '..FFFFFFFFFFFFFFFFFFFFFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFSSSSSSSSSSSSSSSSSSFF..',
    '..FFFFFFFFFFFFFFFFFFFFFF..',
    '...........FFFF...........',
    '.........FFFFFFFF.........',
    'DDDDDDDDDDDDDDDDDDDDDDDDDD',
    'DD......................DD',
    'DD......................DD',
]
PC_PALETTE = {'F': '#3A3F47', 'S': '#A8DCF0', 'D': '#C98F4E'}

YOTA_GRID = [
    '................',
    '......DDDD......',
    '....DDDDDDDD....',
    '...DDDDDDDDDD...',
    '...DDDDDDDDDD...',
    '...DDDDDDDDDD...',
    '....DDDDDDDD....',
    '......DDDD......',
    '.......DD.......',
    '.....DDDDDD.....',
    '...DDDDDDDDDD...',
    '..DDDDDDDDDDDD..',
    '.DDDDDDDDDDDDDD.',
    '.DDDDDDDDDDDDDD.',
    '.DDDDDDDDDDDDDD.',
]
YOTA_PALETTE = {'D': '#7E8CA3'}

DESK_GRID = [
    '............',
    '.....KK.....',
    '....KYYK....',
    '...YYYYYY...',
    '..YYYYYYYY..',
    '..YYYYYYYY..',
    '..YYYYYYYY..',
    '.DDDDDDDDDD.',
    'DDDDDDDDDDDD',
    '............',
]
DESK_PALETTE = {'K': '#8A6D2F', 'Y': '#E8C86A', 'D': '#7A6238'}
