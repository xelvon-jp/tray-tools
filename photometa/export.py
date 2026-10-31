# photometa/export.py
# ★の結果を外へ出す（Qt に依存しない）。
#
#   collect(root, 式, 設定)          … root 配下を再帰で歩いて、式に合う写真を集める
#   sync_frame(写真, 同期先, ...)    … フォトフレーム用に縮小した JPEG を同期先へ足し引きする
#   collect_mitene(写真, 集める先)   … みてね用に、そのままコピーして集める
#   finish_mitene(集める先, session) … 集めた元の写真に「みてね済」を付け、集めた先から消す
#
# どれも時間がかかるので、呼ぶ側は裏のスレッドから呼ぶ。中止は cancel（threading.Event）、
# 進み具合は progress(済んだ数, 全体の数, 名前)（全体が分からないときは 0）。
#
# フォトフレーム同期の決まり（いちばん大事なのは「手で入れた写真に触らない」こと）：
#   - 同期先ごとに台帳（ledger_dir の JSON）を持ち、「同期先のファイル名 ← 元の写真のパス・
#     更新日時・サイズ・縮小の設定・作ったファイルのサイズ」を記録する。足すのも消すのも
#     台帳にあるものだけ。台帳に無いファイル（手で入れた写真）は消さない・上書きしない。
#     名前がぶつかったら、こちらが連番にずれる
#   - 台帳にあっても、同期先のファイルのサイズが台帳の記録と違えば（手で差し替えられた）
#     上書きも削除もしない
#   - 元の写真か設定が変わったものだけ作り直す。作るのは1回で、全部の同期先に同じものを置く
#   - 一時ファイルに書いてから置き換える（途中で止まっても半端なファイルを残さない）
#   - ★が下がって対象から外れた写真は、台帳にあるものだけ消す（ゴミ箱でなく削除）。
#     scope を渡したら、その下の写真だけを消す対象にする（別のフォルダから同期した分を
#     消さないため）。keep の下（★が読めなかった写真・読めなかったフォルダ）も消さない。
#     中止したときは消さない
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field

from . import config as _config
from . import filter_expr as _fe
from .xmp import is_photo, is_raw, read_meta

# 降りないフォルダ（大文字小文字は区別しない）。extract_ で始まるものも降りない
EXCLUDED_DIR_NAMES = frozenset({"print", "delete_candidates", "bestshot"})
EXCLUDED_DIR_PREFIXES = ("extract_",)

LEDGER_VERSION = 1
FRAME_FORMAT = 1              # 作り方を変えたら上げる（全部作り直しになる）
MITENE_MANIFEST = "photometa_mitene.json"

HEIC_EXTS = frozenset({".heic", ".heif"})

# 残す日時のタグ（IFD0 と Exif IFD）
_IFD0_KEEP = (0x0132,)                        # DateTime
_EXIF_KEEP = (0x9003, 0x9004, 0x9010, 0x9011, 0x9012, 0x9290, 0x9291, 0x9292)
#             DateTimeOriginal / Digitized / OffsetTime* / SubSecTime*
_EXIF_IFD, _GPS_IFD = 0x8769, 0x8825


# ------------------------------------------------------------------ 小物

def _key(path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _under(path_key: str, root_key: str) -> bool:
    """path_key が root_key そのものか、その下か（どちらも _key 済み）。"""
    if path_key == root_key:
        return True
    root = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return path_key.startswith(root)


def _cancelled(cancel) -> bool:
    return cancel is not None and cancel.is_set()


def _notify(progress, done, total, name) -> None:
    if progress is None:
        return
    try:
        progress(done, total, name)
    except Exception:  # noqa: BLE001  知らせる側の失敗で止めない
        pass


def _write_json(path: str, data) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(text.encode("utf-8"))
    os.replace(tmp, path)


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None


def _size(path: str) -> int | None:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _unique_name(name: str, taken: set) -> str:
    """taken（casefold した名前の集合）とぶつからない名前。ぶつかったら _2, _3 …"""
    if name.casefold() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    n = 2
    while f"{stem}_{n}{ext}".casefold() in taken:
        n += 1
    return f"{stem}_{n}{ext}"


def _copy_replace(src: str, dest: str) -> None:
    """src を dest へ（同じフォルダの一時ファイルに書いてから置き換える）。更新日時も写す。"""
    folder, name = os.path.split(dest)
    tmp = os.path.join(folder, f".{name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ 式・場所

def frame_expr(config: dict) -> str:
    """フォトフレーム同期で集める式。段階「フォトフレーム用」のしきい値（無ければ ★>=4）。"""
    mn = _config.level_min(config, "フォトフレーム用")
    return f"★>={mn if mn is not None else 4}"


def mitene_keyword(config: dict) -> str | None:
    """「みてね済」に当たるキーワード（設定の keywords の1つ目）。無ければ None。"""
    kws = config.get("keywords") or []
    return kws[0]["name"] if kws else None


def mitene_expr(config: dict) -> str:
    """みてね用に集める式。プリセット「みてね未」があればその式、無ければ
    一番上の段階 && !#みてね済（段階名から作る）。"""
    for p in config.get("presets") or []:
        if p.get("name") == "みてね未" and p.get("expr"):
            return p["expr"]
    levels = _config.levels_by_min(config)
    head = (_fe.atom_text(_fe.Atom("level", levels[0]["name"])) if levels else "★=5")
    kw = mitene_keyword(config)
    if kw:
        return f"{head} && !{_fe.atom_text(_fe.Atom('keyword', kw))}"
    return head


def default_mitene_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "みてね用")


def mitene_dir(config: dict) -> str:
    """みてね用に集める先（設定の mitene_dir。空なら %TEMP%\\みてね用）。"""
    value = config.get("mitene_dir") or ""
    if isinstance(value, str) and value.strip():
        return os.path.abspath(os.path.expandvars(value.strip().strip('"')))
    return default_mitene_dir()


def default_ledger_dir() -> str:
    return os.path.join(_config.data_dir(), "frame_ledger")


def frame_settings(settings) -> dict:
    return _config.normalize_frame(settings)


# ------------------------------------------------------------------ 集める

@dataclass
class CollectResult:
    paths: list = field(default_factory=list)      # 式に合った写真（パス順）
    metas: dict = field(default_factory=dict)      # パス → PhotoMeta（合ったものだけ）
    scanned: int = 0                               # 見た写真の数
    unknown: list = field(default_factory=list)    # ★が読めなかった写真
    error_dirs: list = field(default_factory=list)  # 中を読めなかったフォルダ
    cancelled: bool = False


def excluded_dir_name(name: str) -> bool:
    folded = name.casefold()
    return (folded in EXCLUDED_DIR_NAMES or folded.startswith(EXCLUDED_DIR_PREFIXES)
            or name.startswith("."))


def _skip_dir(entry) -> bool:
    if excluded_dir_name(entry.name):
        return True
    try:
        if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
            return True
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM
                | stat.FILE_ATTRIBUTE_REPARSE_POINT):
        return True
    return False


def _excluded_roots(config: dict | None) -> set:
    """降りない場所（同期先そのもの・みてね用に集める先）。"""
    cfg = config or {}
    out = {_key(t) for t in cfg.get("frame_targets") or [] if isinstance(t, str) and t}
    out.add(_key(mitene_dir(cfg)))
    return out


def collect(root, expr_or_node, config: dict | None = None, cancel=None,
            progress=None) -> CollectResult:
    """root 配下を再帰で歩き、写真の★を読んで、式に合うものを返す。

    expr_or_node は式の文字列か filter_expr の構文木（None なら写真全部）。
    print / delete_candidates / bestshot / extract_* / 隠しフォルダ / ジャンクション・
    シンボリックリンク / 同期先（frame_targets）/ みてね用に集める先 には降りない。
    progress(見た写真の数, 0, 今のフォルダ) で知らせる。
    """
    cfg = config if config is not None else _config.load_config()
    node = _fe.parse(expr_or_node) if isinstance(expr_or_node, str) else expr_or_node
    result = CollectResult()
    skip = _excluded_roots(cfg)
    stack = [os.path.abspath(os.fspath(root))]
    while stack:
        if _cancelled(cancel):
            result.cancelled = True
            break
        folder = stack.pop()
        if _key(folder) in skip:
            continue
        _notify(progress, result.scanned, 0, folder)
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name.casefold())
        except OSError:
            result.error_dirs.append(folder)
            continue
        subdirs = []
        for entry in entries:
            if _cancelled(cancel):
                result.cancelled = True
                break
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if not _skip_dir(entry) and _key(entry.path) not in skip:
                    subdirs.append(entry.path)
                continue
            if not is_photo(entry.name):
                continue
            result.scanned += 1
            meta = read_meta(entry.path)
            if meta is None:
                result.unknown.append(entry.path)
                continue
            if _fe.evaluate(node, _fe.meta_atom_fn(meta, cfg)) is True:
                result.paths.append(entry.path)
                result.metas[entry.path] = meta
        stack.extend(reversed(subdirs))
    result.paths.sort(key=lambda p: _key(p))
    return result


# ------------------------------------------------------------------ 撮影日時

def _stamp_from_exif(exif) -> str | None:
    """EXIF の日時（DateTimeOriginal → DateTimeDigitized → DateTime）を YYYYMMDD_HHMMSS に。"""
    values = []
    try:
        sub = exif.get_ifd(_EXIF_IFD)
        values += [sub.get(0x9003), sub.get(0x9004)]
    except Exception:  # noqa: BLE001
        pass
    values.append(exif.get(0x0132))
    for v in values:
        if isinstance(v, bytes):
            v = v.decode("ascii", "replace")
        if not isinstance(v, str):
            continue
        digits = "".join(c for c in v if c.isdigit())
        if len(digits) >= 14 and digits[:14] != "0" * 14:
            return f"{digits[:8]}_{digits[8:14]}"
    return None


def _mtime_stamp(path: str) -> str:
    try:
        t = os.stat(path).st_mtime
    except OSError:
        t = time.time()
    return _dt.datetime.fromtimestamp(t).strftime("%Y%m%d_%H%M%S")


_heif_state: list = []


def heif_available() -> bool:
    """pillow_heif が使えるか（使えれば Pillow に HEIC の読み込みを登録する）。"""
    if not _heif_state:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            _heif_state.append(True)
        except Exception:  # noqa: BLE001  入っていない・壊れている
            _heif_state.append(False)
    return _heif_state[0]


def _is_heic(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in HEIC_EXTS


def photo_stamp(path: str) -> str:
    """名前に使う撮影日時（YYYYMMDD_HHMMSS）。EXIF に無ければ更新日時。"""
    try:
        from PIL import Image
        if _is_heic(path):
            heif_available()
        with Image.open(path) as im:
            stamp = _stamp_from_exif(im.getexif())
        if stamp:
            return stamp
    except Exception:  # noqa: BLE001  読めなければ更新日時
        pass
    return _mtime_stamp(path)


# ------------------------------------------------------------------ フレーム用 JPEG

def build_frame_jpeg(src: str, out_path: str, settings: dict) -> str:
    """フレーム用の JPEG を out_path に作る。名前に使う撮影日時（YYYYMMDD_HHMMSS）を返す。

    長辺を max_long_edge 以下に縮小（拡大しない・LANCZOS）、quality で保存。EXIF の向きは
    画素を回して直し、向き情報は付けない。日時のタグは残し、GPS は strip_gps なら消す
    （ほかの EXIF は付けない）。色の ICC プロファイルは残す。読めなければ例外。
    """
    from PIL import Image, ImageOps

    s = frame_settings(settings)
    if _is_heic(src):
        heif_available()
    with Image.open(src) as im:
        exif = im.getexif()
        stamp = _stamp_from_exif(exif)
        icc = im.info.get("icc_profile")
        img = ImageOps.exif_transpose(im)
        if img is None:
            img = im
        img.load()
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            base = Image.new("RGB", rgba.size, (255, 255, 255))
            base.paste(rgba, mask=rgba.getchannel("A"))
            img = base
        elif img.mode != "RGB":
            if img.mode == "CMYK":
                icc = None          # CMYK 用のプロファイルは RGB には合わない
            img = img.convert("RGB")
        w, h = img.size
        edge = s["max_long_edge"]
        if max(w, h) > edge:
            scale = edge / max(w, h)
            size = (max(1, round(w * scale)), max(1, round(h * scale)))
            img = img.resize(size, Image.LANCZOS)
        out = Image.Exif()
        for tag in _IFD0_KEEP:
            if tag in exif:
                out[tag] = exif[tag]
        try:
            sub = exif.get_ifd(_EXIF_IFD)
        except Exception:  # noqa: BLE001
            sub = {}
        kept = {tag: sub[tag] for tag in _EXIF_KEEP if tag in sub}
        if kept:
            out.get_ifd(_EXIF_IFD).update(kept)
        if not s["strip_gps"]:
            try:
                gps = exif.get_ifd(_GPS_IFD)
            except Exception:  # noqa: BLE001
                gps = {}
            if gps:
                out.get_ifd(_GPS_IFD).update(gps)
        kwargs = {"quality": s["quality"], "optimize": True, "exif": out.tobytes()}
        if icc:
            kwargs["icc_profile"] = icc
        img.save(out_path, "JPEG", **kwargs)
    return stamp or _mtime_stamp(src)


# ------------------------------------------------------------------ 台帳

def ledger_path(ledger_dir: str, target: str) -> str:
    digest = hashlib.sha1(_key(target).encode("utf-8")).hexdigest()[:16]
    return os.path.join(ledger_dir, f"frame_{digest}.json")


def load_ledger(ledger_dir: str, target: str) -> dict:
    """同期先の台帳（名前 → 記録）。無い・壊れていれば空（＝何も消さない側に倒れる）。"""
    data = _read_json(ledger_path(ledger_dir, target))
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    out = {}
    for name, rec in files.items():
        if (isinstance(name, str) and isinstance(rec, dict)
                and isinstance(rec.get("source"), str)
                and isinstance(rec.get("out_size"), int)):
            out[name] = rec
    return out


def save_ledger(ledger_dir: str, target: str, files: dict) -> None:
    _write_json(ledger_path(ledger_dir, target),
                {"version": LEDGER_VERSION, "target": os.path.abspath(target), "files": files})


# ------------------------------------------------------------------ フォトフレーム同期

@dataclass
class SyncResult:
    added: list = field(default_factory=list)      # (同期先, 名前, 元の写真)
    updated: list = field(default_factory=list)
    deleted: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    failed: list = field(default_factory=list)     # (パス, 理由)
    kept: list = field(default_factory=list)       # (同期先のファイル, 理由) 手で差し替えられていた
    skipped_heic: list = field(default_factory=list)
    skipped_raw: list = field(default_factory=list)
    cancelled: bool = False
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return self.error
        text = (f"追加 {len(self.added)}・更新 {len(self.updated)}・削除 {len(self.deleted)}"
                f"・変更なし {len(self.unchanged)}")
        if self.failed:
            text += f"・失敗 {len(self.failed)}"
        if self.cancelled:
            text += "（中止しました。消すのはしていません）"
        return text

    def notes(self) -> list[str]:
        out = []
        if self.skipped_heic:
            out.append(f"HEIC は読めないので飛ばしました（{len(self.skipped_heic)} 枚。"
                       "pillow-heif を入れると読めます）")
        if self.skipped_raw:
            out.append(f"RAW は書き出せないので飛ばしました（{len(self.skipped_raw)} 枚）")
        if self.kept:
            out.append(f"手で差し替えられていたので触りませんでした（{len(self.kept)} 件）")
        for path, reason in self.failed[:5]:
            out.append(f"失敗 {os.path.basename(path)}：{reason}")
        if len(self.failed) > 5:
            out.append(f"ほか {len(self.failed) - 5} 件失敗")
        return out


class _Target:
    def __init__(self, folder: str, ledger: dict):
        self.dir = folder
        self.ledger = ledger
        self.by_source = {_key(rec["source"]): name for name, rec in ledger.items()}
        try:
            names = os.listdir(folder)
        except OSError:
            names = []
        self.taken = {n.casefold() for n in names} | {n.casefold() for n in ledger}


def sync_frame(sources, targets, settings, ledger_dir: str, cancel=None, progress=None,
               scope=None, keep=()) -> SyncResult:
    """フォトフレーム同期。sources（写真のパス）を、縮小した JPEG にして targets の各フォルダへ。

    台帳は ledger_dir に同期先ごと。scope（フォルダの並び）を渡したら、その下の写真だけを
    「対象から外れたので消す」候補にする。keep（ファイルかフォルダ）の下は消さない。
    progress(済んだ数, 全体, 名前)。
    """
    result = SyncResult()
    targets = [os.path.abspath(t) for t in (targets or []) if isinstance(t, str) and t.strip()]
    if not targets:
        result.error = "同期先がありません（photo_ratings.json の frame_targets）"
        return result
    s = frame_settings(settings)
    sig = dict(s, format=FRAME_FORMAT)
    order, seen = [], set()
    for p in sources:
        p = os.path.abspath(os.fspath(p))
        k = _key(p)
        if k not in seen:
            seen.add(k)
            order.append(p)
    states: list[_Target] = []
    for t in targets:
        try:
            os.makedirs(t, exist_ok=True)
        except OSError as e:
            result.failed.append((t, f"同期先を作れません（{e}）"))
            continue
        states.append(_Target(t, load_ledger(ledger_dir, t)))
    if not states:
        result.error = "同期先を1つも使えません" + (
            f"（{result.failed[0][1]}）" if result.failed else "")
        return result
    scope_keys = None if scope is None else [_key(p) for p in scope]
    keep_keys = [_key(p) for p in keep]
    heif_ok = None
    total = len(order)
    work = tempfile.mkdtemp(prefix="photometa_frame_")
    try:
        for i, src in enumerate(order):
            if _cancelled(cancel):
                result.cancelled = True
                break
            _notify(progress, i, total, os.path.basename(src))
            key = _key(src)
            try:
                st = os.stat(src)
            except OSError:
                result.failed.append((src, "元の写真がありません"))
                continue
            plans = []
            for t in states:
                name = t.by_source.get(key)
                if name is None:
                    plans.append((t, None, "add"))
                    continue
                rec = t.ledger[name]
                have = _size(os.path.join(t.dir, name))
                if have is None:
                    plans.append((t, name, "add"))          # 同期先から消えていた → 置き直す
                elif have != rec["out_size"]:
                    result.kept.append((os.path.join(t.dir, name), "手で差し替えられています"))
                elif (rec.get("mtime_ns") == st.st_mtime_ns and rec.get("size") == st.st_size
                      and rec.get("settings") == sig):
                    result.unchanged.append((t.dir, name, src))
                else:
                    plans.append((t, name, "update"))
            if not plans:
                continue
            if is_raw(src):
                result.skipped_raw.append(src)
                continue
            if _is_heic(src):
                if heif_ok is None:
                    heif_ok = heif_available()
                if not heif_ok:
                    result.skipped_heic.append(src)
                    continue
            built = os.path.join(work, f"{i}.jpg")
            try:
                stamp = build_frame_jpeg(src, built, s)
            except Exception as e:  # noqa: BLE001  1枚の失敗で止めない
                result.failed.append((src, f"{type(e).__name__}: {e}"))
                continue
            stem = os.path.splitext(os.path.basename(src))[0]
            for t, name, action in plans:
                if name is None:
                    name = _unique_name(f"{stamp}_{stem}.jpg", t.taken)
                dest = os.path.join(t.dir, name)
                if action == "update":
                    # 作っている間に手で差し替えられていないかをもう一度見る
                    have = _size(dest)
                    if have is not None and have != t.ledger[name]["out_size"]:
                        result.kept.append((dest, "手で差し替えられています"))
                        continue
                elif os.path.lexists(dest) and t.by_source.get(key) != name:
                    # 新しい名前の置き場に、いつの間にか別のファイルができていた
                    name = _unique_name(name, t.taken | {name.casefold()})
                    dest = os.path.join(t.dir, name)
                try:
                    _copy_replace(built, dest)
                    out_size = os.stat(dest).st_size
                except OSError as e:
                    result.failed.append((dest, f"書けません（{e}）"))
                    continue
                t.ledger[name] = {"source": src, "mtime_ns": st.st_mtime_ns, "size": st.st_size,
                                  "settings": sig, "out_size": out_size}
                t.by_source[key] = name
                t.taken.add(name.casefold())
                (result.updated if action == "update" else result.added).append((t.dir, name, src))
            try:
                os.remove(built)
            except OSError:
                pass
        if not result.cancelled and not _cancelled(cancel):
            _notify(progress, total, total, "対象から外れた写真を片付けています")
            for t in states:
                for name, rec in list(t.ledger.items()):
                    skey = _key(rec["source"])
                    if skey in seen:
                        continue
                    if scope_keys is not None and not any(_under(skey, r) for r in scope_keys):
                        continue
                    if any(_under(skey, k) for k in keep_keys):
                        continue
                    dest = os.path.join(t.dir, name)
                    have = _size(dest)
                    if have is not None and have != rec["out_size"]:
                        # 手で差し替えられたもの: 消さず、台帳からも外す（以後は手で入れた扱い）
                        result.kept.append((dest, "手で差し替えられていたので消しませんでした"))
                    elif have is not None:
                        try:
                            os.remove(dest)
                        except OSError as e:
                            result.failed.append((dest, f"消せません（{e}）"))
                            continue
                        result.deleted.append((t.dir, name, rec["source"]))
                    del t.ledger[name]
                    t.by_source.pop(skey, None)
        elif not result.cancelled:
            result.cancelled = True
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for t in states:
            try:
                save_ledger(ledger_dir, t.dir, t.ledger)
            except OSError as e:
                result.failed.append((ledger_path(ledger_dir, t.dir), f"台帳を書けません（{e}）"))
    return result


# ------------------------------------------------------------------ みてね

@dataclass
class MiteneResult:
    copied: list = field(default_factory=list)     # (集めた先の名前, 元の写真)
    already: list = field(default_factory=list)    # もう集めてあった元の写真
    failed: list = field(default_factory=list)     # (パス, 理由)
    cancelled: bool = False
    dest: str = ""


@dataclass
class FinishResult:
    marked: list = field(default_factory=list)     # 「みてね済」を付けた元の写真
    removed: list = field(default_factory=list)    # 集めた先から消したコピー
    missing: list = field(default_factory=list)    # 集めた先から消されていた（付けない）元の写真
    failed: list = field(default_factory=list)     # (パス, 理由)
    results: list = field(default_factory=list)    # writer.WriteResult（表示の反映用）
    cancelled: bool = False
    error: str | None = None


def manifest_path(dest: str) -> str:
    return os.path.join(dest, MITENE_MANIFEST)


def load_manifest(dest: str) -> list:
    """集めた先の一覧（[{"name", "source", "size", "mtime_ns"}]）。無ければ空。"""
    data = _read_json(manifest_path(dest))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict) and isinstance(it.get("name"), str)
            and isinstance(it.get("source"), str)]


def save_manifest(dest: str, items: list) -> None:
    path = manifest_path(dest)
    if not items:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    _write_json(path, {"version": 1, "items": items})


def collect_mitene(sources, dest: str, cancel=None, progress=None) -> MiteneResult:
    """sources（式で選んだ写真）を dest にそのままコピーする（縮小しない）。

    名前は「撮影日時_元の名前」（ぶつかったら連番）。dest に集めた元のパスの一覧
    （MITENE_MANIFEST）を置く。もう集めてあって、コピーが残っているものは飛ばす。
    """
    dest = os.path.abspath(dest)
    result = MiteneResult(dest=dest)
    os.makedirs(dest, exist_ok=True)
    items = load_manifest(dest)
    have = {}
    for it in items:
        if os.path.exists(os.path.join(dest, it["name"])):
            have[_key(it["source"])] = it
    items = list(have.values())          # コピーが消えていた記録は捨てる（集め直す）
    try:
        taken = {n.casefold() for n in os.listdir(dest)}
    except OSError:
        taken = set()
    sources = [os.path.abspath(os.fspath(p)) for p in sources]
    try:
        for i, src in enumerate(sources):
            if _cancelled(cancel):
                result.cancelled = True
                break
            _notify(progress, i, len(sources), os.path.basename(src))
            key = _key(src)
            if key in have:
                result.already.append(src)
                continue
            try:
                st = os.stat(src)
            except OSError:
                result.failed.append((src, "元の写真がありません"))
                continue
            name = _unique_name(f"{photo_stamp(src)}_{os.path.basename(src)}", taken)
            try:
                _copy_replace(src, os.path.join(dest, name))
            except OSError as e:
                result.failed.append((src, f"コピーできません（{e}）"))
                continue
            taken.add(name.casefold())
            it = {"name": name, "source": src, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
            items.append(it)
            have[key] = it
            result.copied.append((name, src))
    finally:
        save_manifest(dest, items)
    return result


def finish_mitene(dest: str, session, keyword: str, cancel=None, progress=None) -> FinishResult:
    """dest の一覧にある元の写真へ keyword（みてね済）を付け、成功したものだけ dest から消す。

    集めた先からコピーが消されていた写真には付けない（上げないことにしたもの）。
    session は writer.ExifToolSession（set_keyword を持つもの）。
    """
    dest = os.path.abspath(dest)
    result = FinishResult()
    if not keyword:
        result.error = "みてね済に当たるキーワードがありません（photo_ratings.json の keywords）"
        return result
    items = load_manifest(dest)
    remaining = []
    try:
        for i, it in enumerate(items):
            if _cancelled(cancel):
                result.cancelled = True
                remaining.extend(items[i:])
                break
            _notify(progress, i, len(items), it["name"])
            copy = os.path.join(dest, it["name"])
            src = it["source"]
            if not os.path.exists(copy):
                result.missing.append(src)
                continue
            if not os.path.isfile(src):
                result.failed.append((src, "元の写真がありません"))
                remaining.append(it)
                continue
            try:
                r = session.set_keyword([src], keyword, True)[0]
            except Exception as e:  # noqa: BLE001  ExifTool が落ちたなど
                result.failed.append((src, f"{type(e).__name__}: {e}"))
                remaining.append(it)
                continue
            result.results.append(r)
            if not r.ok:
                result.failed.append((src, r.error or "書けませんでした"))
                remaining.append(it)
                continue
            result.marked.append(src)
            try:
                os.remove(copy)
                result.removed.append(copy)
            except OSError as e:
                result.failed.append((copy, f"集めた先から消せません（{e}）"))
                remaining.append(it)       # 付け直しても害はないので、次の片付けで消す
    finally:
        try:
            save_manifest(dest, remaining)
        except OSError as e:
            result.failed.append((manifest_path(dest), f"一覧を書けません（{e}）"))
    return result
