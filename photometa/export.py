# photometa/export.py
# ★の結果を外へ出す（Qt に依存しない）。
#
#   collect(root, 式, 設定)          … root 配下を再帰で歩いて、式に合う写真を集める
#   sync_frame(写真, 同期先, ...)    … フォトフレーム用に縮小した JPEG を同期先へ足し引きする
#   plan_frame_sync(写真, 同期先, ...) … sync_frame の予行（何も書かずに、足す・消す数を数える）
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
#
# 削除がクラウドへ反映されない同期先（設定の frame_manual_delete。Amazon Photos のデスクトップ
# アプリが自動バックアップしているフォルダなど。足した写真は上がるが、消してもクラウドに残る）：
#   - そこから消した写真を「クラウドで手で消す写真」として記録する（manual_delete_path の JSON）。
#     項目は {同期先フォルダ, ファイル名, 元の写真のパス, 消した日時}
#   - 同じ写真をまた同じ同期先に入れ直したら、その項目は外す（クラウドにはまだ残っているので、
#     消す必要が無くなる）
#   - 一覧は pending_manual_deletes()、手で消し終わったら clear_manual_deletes() で外す
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
from . import metacache as _metacache
from .xmp import is_photo, is_raw, read_meta

# 降りないフォルダ（大文字小文字は区別しない）。extract_ で始まるものも降りない
EXCLUDED_DIR_NAMES = frozenset({"print", "delete_candidates", "bestshot"})
EXCLUDED_DIR_PREFIXES = ("extract_",)

LEDGER_VERSION = 1
FRAME_FORMAT = 1              # 作り方を変えたら上げる（全部作り直しになる）
MITENE_MANIFEST = "photometa_mitene.json"
MANUAL_DELETE_FILE = "frame_manual_delete.json"
MANUAL_DELETE_VERSION = 1

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

def photo_edits(path: str):
    """写真の補正（crs:）を読む（読み取りキャッシュ経由。読めなければ None）。"""
    try:
        meta = _metacache.read_meta_cached(path)
    except Exception:  # noqa: BLE001  補正が読めなくても同期は続ける
        return None
    return meta.edits if meta else None


def _faces_sig(faces) -> str:
    """顔の並び（(x, y, w, h) の比率）を署名用の文字列にする。"""
    try:
        return ";".join(",".join(f"{float(v):.3f}" for v in box) for box in faces)
    except (TypeError, ValueError):
        return ""


def _sig_with_edits(sig: dict, edits) -> dict:
    """台帳の署名に、その写真の補正を含めたもの。補正が無ければ sig のまま
    （既存の台帳の記録と一致し続けるので、補正の無い写真は作り直しにならない）。"""
    if edits is None:
        return sig
    return dict(sig, edits=edits.signature())


def build_frame_jpeg(src: str, out_path: str, settings: dict, edits=None,
                     faces=None) -> str:
    """フレーム用の JPEG を out_path に作る。名前に使う撮影日時（YYYYMMDD_HHMMSS）を返す。

    長辺を max_long_edge 以下に縮小（拡大しない・LANCZOS）、quality で保存。EXIF の向きは
    画素を回して直し、向き情報は付けない。日時のタグは残し、GPS は strip_gps なら消す
    （ほかの EXIF は付けない）。色の ICC プロファイルは残す。読めなければ例外。
    edits（補正）があれば、縮小の前に当てる（向き→傾き→切り抜き→明るさ）。
    設定 crop_aspect があれば、手動の切り抜きが無い写真をその縦横比に切り抜く
    （faces があれば顔基準、無ければ中央。render.aspect_crop）。
    """
    from PIL import Image, ImageOps

    s = frame_settings(settings)
    if _is_heic(src):
        heif_available()
    with Image.open(src) as im:
        exif = im.getexif()
        stamp = _stamp_from_exif(exif)
        icc = im.info.get("icc_profile")
        if edits is not None and not edits.is_empty():
            from . import render
            img = render.apply_edits(im, edits)
        else:
            img = ImageOps.exif_transpose(im)
            if img is None:
                img = im
        img.load()
        if s.get("crop_aspect") and not (edits is not None and edits.crop is not None):
            from . import render
            crop = render.aspect_crop(img.size[0], img.size[1], s["crop_aspect"],
                                      faces=faces)
            if crop is not None:
                top, left, bottom, right = crop
                w, h = img.size
                img = img.crop((round(left * w), round(top * h),
                                round(right * w), round(bottom * h)))
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


# ------------------------------------------------------------------ 手で消す写真の一覧

def manual_delete_path(ledger_dir: str | None = None) -> str:
    """「クラウドで手で消す写真」の記録の場所。台帳のフォルダの隣（既定なら
    %LOCALAPPDATA%\\traytools\\frame_manual_delete.json）。

    ledger_dir を省くと default_ledger_dir() を呼んで決める（テストは default_ledger_dir を
    一時フォルダへ差し替えれば、この記録も一時フォルダに入る）。"""
    folder = ledger_dir if ledger_dir else default_ledger_dir()
    return os.path.join(os.path.dirname(os.path.abspath(folder)), MANUAL_DELETE_FILE)


def _manual_item(rec) -> dict | None:
    """記録の1項目を確かめる（壊れていれば None）。"""
    if not isinstance(rec, dict):
        return None
    out = {}
    for k in ("target", "name", "source", "deleted_at"):
        v = rec.get(k)
        if not isinstance(v, str) or not v:
            return None
        out[k] = v
    return out


def _manual_ident(item: dict) -> tuple:
    return (_key(item["target"]), item["name"].casefold(), _key(item["source"]),
            item["deleted_at"])


def _manual_sort_key(item: dict) -> tuple:
    return (_key(item["target"]), item["deleted_at"], item["name"].casefold())


def _load_manual(path: str) -> list:
    data = _read_json(path)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [it for it in (_manual_item(r) for r in items) if it is not None]


def _save_manual(path: str, items: list) -> None:
    _write_json(path, {"version": MANUAL_DELETE_VERSION,
                       "items": sorted(items, key=_manual_sort_key)})


def pending_manual_deletes(ledger_dir: str | None = None) -> list:
    """今の「クラウドで手で消す写真」の一覧。同期先ごと・消した日時の順。
    項目は {"target": 同期先フォルダ, "name": ファイル名, "source": 元の写真, "deleted_at": 日時}。"""
    return sorted(_load_manual(manual_delete_path(ledger_dir)), key=_manual_sort_key)


def clear_manual_deletes(items=None, ledger_dir: str | None = None) -> int:
    """一覧から items（pending_manual_deletes の項目）を外す。None なら全部。外した数を返す。"""
    path = manual_delete_path(ledger_dir)
    current = _load_manual(path)
    if items is None:
        keep = []
    else:
        drop = {_manual_ident(it) for it in (_manual_item(r) for r in items) if it is not None}
        keep = [it for it in current if _manual_ident(it) not in drop]
    removed = len(current) - len(keep)
    if removed:
        _save_manual(path, keep)
    return removed


def _update_manual(path: str, added: list, readded: set) -> None:
    """記録を読み直して、入れ直した分（(同期先, 元の写真) の _key の組）を外し、added を足す。
    同期のあいだにもう片方のアプリが書いた分を消さないよう、書く直前に読む。"""
    current = _load_manual(path)
    items = [it for it in current
             if (_key(it["target"]), _key(it["source"])) not in readded]
    seen = {_manual_ident(it) for it in items}
    for it in added:
        if _manual_ident(it) not in seen:
            items.append(it)
            seen.add(_manual_ident(it))
    if items != current:
        _save_manual(path, items)


def manual_delete_report(result, ledger_dir: str | None = None, limit: int | None = None) -> list:
    """同期の結果のうち、今回足した「クラウドで手で消す写真」を知らせる行。無ければ空。

    同期先ごとに「Amazon 側（フォルダ名）で手で消す写真：n枚（いま全部で m枚）」と、
    今回分のファイル名（limit を渡したらその数まで。残りは「ほか n 枚」）。"""
    added = list(getattr(result, "manual_delete", None) or [])
    if not added:
        return []
    totals: dict = {}
    for it in pending_manual_deletes(ledger_dir):
        k = _key(it["target"])
        totals[k] = totals.get(k, 0) + 1
    groups: dict = {}
    for target, name, _src in added:
        groups.setdefault(_key(target), (target, []))[1].append(name)
    lines = []
    for k, (target, names) in groups.items():
        folder = os.path.basename(os.path.normpath(target)) or target
        lines.append(f"Amazon 側（{folder}）で手で消す写真：{len(names)}枚"
                     f"（いま全部で {totals.get(k, 0)}枚）")
        shown = names if limit is None else names[:limit]
        lines += [f"  {n}" for n in shown]
        if len(names) > len(shown):
            lines.append(f"  ほか {len(names) - len(shown)} 枚")
    return lines


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
    # (同期先, 名前, 元の写真) 削除がクラウドへ反映されない同期先から消したので、今回
    # 「クラウドで手で消す写真」の一覧に足したもの
    manual_delete: list = field(default_factory=list)
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


# sync_frame と plan_frame_sync が同じ判定をするための共通の部品（どれもファイルには書かない）

def _unique_sources(sources) -> tuple[list, set]:
    """写真のパスを abspath にして、重複を除いた並びと、その _key の集合。"""
    order, seen = [], set()
    for p in sources:
        p = os.path.abspath(os.fspath(p))
        k = _key(p)
        if k not in seen:
            seen.add(k)
            order.append(p)
    return order, seen


def _clean_targets(targets) -> list:
    return [os.path.abspath(t) for t in (targets or []) if isinstance(t, str) and t.strip()]


def _manual_keys(manual_delete) -> set:
    return {_key(p) for p in (manual_delete or ()) if isinstance(p, str) and p.strip()}


def _frame_action(t: _Target, key: str, st, sig: dict) -> tuple[str, str | None]:
    """元の写真1枚（_key と os.stat の結果）を同期先 t でどうするか。(動作, 同期先の名前) を返す。

    "add"（名前が None ならまだ無い。名前があれば同期先から消えていたので置き直す）/
    "update"（元の写真か設定が変わった）/ "unchanged"（そのまま）/
    "kept"（手で差し替えられているので触らない）"""
    name = t.by_source.get(key)
    if name is None:
        return "add", None
    rec = t.ledger[name]
    have = _size(os.path.join(t.dir, name))
    if have is None:
        return "add", name
    if have != rec["out_size"]:
        return "kept", name
    if (rec.get("mtime_ns") == st.st_mtime_ns and rec.get("size") == st.st_size
            and rec.get("settings") == sig):
        return "unchanged", name
    return "update", name


def _cannot_build(src: str, heif_cache: list) -> str | None:
    """作れない写真なら "raw" / "heic"、作れるなら None。heif_cache は heif_available() の結果を
    入れておく入れ物（初めて HEIC を見たときに調べる）。"""
    if is_raw(src):
        return "raw"
    if _is_heic(src):
        if not heif_cache:
            heif_cache.append(heif_available())
        if not heif_cache[0]:
            return "heic"
    return None


def _stale_entries(t: _Target, seen: set, scope_keys, keep_keys) -> list:
    """対象から外れたので片付ける台帳の項目。(名前, 記録, 動作) の並び。動作は
    "delete"（消す）/ "kept"（手で差し替えられているので消さない）/ "gone"（もう無い。台帳から外すだけ）。"""
    out = []
    for name, rec in list(t.ledger.items()):
        skey = _key(rec["source"])
        if skey in seen:
            continue
        if scope_keys is not None and not any(_under(skey, r) for r in scope_keys):
            continue
        if any(_under(skey, k) for k in keep_keys):
            continue
        have = _size(os.path.join(t.dir, name))
        if have is None:
            out.append((name, rec, "gone"))
        elif have != rec["out_size"]:
            out.append((name, rec, "kept"))
        else:
            out.append((name, rec, "delete"))
    return out


def sync_frame(sources, targets, settings, ledger_dir: str, cancel=None, progress=None,
               scope=None, keep=(), manual_delete=(), faces_provider=None) -> SyncResult:
    """フォトフレーム同期。sources（写真のパス）を、縮小した JPEG にして targets の各フォルダへ。

    台帳は ledger_dir に同期先ごと。scope（フォルダの並び）を渡したら、その下の写真だけを
    「対象から外れたので消す」候補にする。keep（ファイルかフォルダ）の下は消さない。
    manual_delete（フォルダの並び。設定の frame_manual_delete）に入っている同期先から消したものは、
    「クラウドで手で消す写真」の一覧（manual_delete_path(ledger_dir)）に足す。
    progress(済んだ数, 全体, 名前)。
    """
    result = SyncResult()
    targets = _clean_targets(targets)
    if not targets:
        result.error = "同期先がありません（photo_ratings.json の frame_targets）"
        return result
    s = frame_settings(settings)
    sig = dict(s, format=FRAME_FORMAT)
    order, seen = _unique_sources(sources)
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
    manual_keys = _manual_keys(manual_delete)
    manual_added: list = []          # 一覧に足す項目
    readded: set = set()             # 入れ直した (同期先, 元の写真)。一覧から外す
    heif_cache: list = []
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
            edits = photo_edits(src)
            # 顔基準の切り抜きが効くときだけ、顔も署名に入れる（顔の解析が
            # 変わったら作り直しになる。手動の切り抜きがある写真では顔は無関係）
            faces = None
            if (faces_provider is not None and s.get("crop_aspect")
                    and not (edits is not None and edits.crop is not None)):
                try:
                    faces = faces_provider(src)
                except Exception:  # noqa: BLE001  顔が取れなくても中央で切り抜く
                    faces = None
            sig_i = _sig_with_edits(sig, edits)
            if faces:
                sig_i = dict(sig_i, faces=_faces_sig(faces))
            plans = []
            for t in states:
                action, name = _frame_action(t, key, st, sig_i)
                if action == "kept":
                    result.kept.append((os.path.join(t.dir, name), "手で差し替えられています"))
                elif action == "unchanged":
                    result.unchanged.append((t.dir, name, src))
                else:
                    plans.append((t, name, action))   # add（消えていたものは置き直す）/ update
            if not plans:
                continue
            skip = _cannot_build(src, heif_cache)
            if skip is not None:
                (result.skipped_raw if skip == "raw" else result.skipped_heic).append(src)
                continue
            built = os.path.join(work, f"{i}.jpg")
            try:
                stamp = build_frame_jpeg(src, built, s, edits=edits, faces=faces)
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
                                  "settings": sig_i, "out_size": out_size}
                t.by_source[key] = name
                t.taken.add(name.casefold())
                (result.updated if action == "update" else result.added).append((t.dir, name, src))
                if action == "add":
                    readded.add((_key(t.dir), key))
            try:
                os.remove(built)
            except OSError:
                pass
        if not result.cancelled and not _cancelled(cancel):
            _notify(progress, total, total, "対象から外れた写真を片付けています")
            for t in states:
                for name, rec, action in _stale_entries(t, seen, scope_keys, keep_keys):
                    skey = _key(rec["source"])
                    dest = os.path.join(t.dir, name)
                    if action == "kept":
                        # 手で差し替えられたもの: 消さず、台帳からも外す（以後は手で入れた扱い）
                        result.kept.append((dest, "手で差し替えられていたので消しませんでした"))
                    elif action == "delete":
                        try:
                            os.remove(dest)
                        except OSError as e:
                            result.failed.append((dest, f"消せません（{e}）"))
                            continue
                        result.deleted.append((t.dir, name, rec["source"]))
                        if _key(t.dir) in manual_keys:
                            manual_added.append({
                                "target": t.dir, "name": name, "source": rec["source"],
                                "deleted_at": _dt.datetime.now().isoformat(timespec="seconds")})
                            result.manual_delete.append((t.dir, name, rec["source"]))
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
        if manual_added or readded:
            mpath = manual_delete_path(ledger_dir)
            try:
                _update_manual(mpath, manual_added, readded)
            except OSError as e:
                result.failed.append((mpath, f"手で消す写真の一覧を書けません（{e}）"))
    return result


# ------------------------------------------------------------------ フォトフレーム同期の予行

@dataclass
class FramePlanTarget:
    """同期先1か所の予行の結果。"""
    target: str                                    # 同期先のフォルダ（abspath）
    added: list = field(default_factory=list)      # 元の写真のパス（同期先の名前は作るときに決まる）
    updated: list = field(default_factory=list)    # (同期先の名前, 元の写真) 元の写真か設定が変わった
    deleted: list = field(default_factory=list)    # (同期先の名前, 元の写真) 対象から外れたので消す
    unchanged: list = field(default_factory=list)  # (同期先の名前, 元の写真)
    kept: list = field(default_factory=list)       # 同期先の名前。手で差し替えられていて触らない
    manual: bool = False                           # 削除がクラウドへ反映されない同期先か

    @property
    def manual_delete(self) -> list:
        """消したあとクラウドで手で消す必要があるもの（manual の同期先の deleted）。"""
        return list(self.deleted) if self.manual else []

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.deleted)

    def summary(self) -> str:
        """「追加 5／更新 0／削除 2（Amazon 側で手で消す 2）／そのまま 16」"""
        deleted = f"削除 {len(self.deleted)}"
        if self.manual_delete:
            deleted += f"（Amazon 側で手で消す {len(self.manual_delete)}）"
        return (f"追加 {len(self.added)}／更新 {len(self.updated)}／{deleted}"
                f"／そのまま {len(self.unchanged)}")


@dataclass
class FramePlan:
    targets: list = field(default_factory=list)       # FramePlanTarget（同期先の順）
    sources: list = field(default_factory=list)       # 元の写真（重複を除いたもの）
    missing: list = field(default_factory=list)       # 元の写真が無い
    skipped_heic: list = field(default_factory=list)  # 作れないので飛ばす（足す・作り直すときだけ）
    skipped_raw: list = field(default_factory=list)
    cancelled: bool = False
    error: str | None = None

    @property
    def changed(self) -> bool:
        """どこかの同期先で、追加・更新・削除のどれかがあるか。"""
        return any(t.changed for t in self.targets)


def plan_frame_sync(sources, targets, settings, ledger_dir: str | None = None, scope=None,
                    keep=(), manual_delete=(), cancel=None, progress=None,
                    faces_provider=None) -> FramePlan:
    """sync_frame の予行。同じ引数で、同期先ごとに追加・更新・削除・そのままを数える。

    判定は sync_frame と同じ部品（_frame_action / _cannot_build / _stale_entries）で行い、
    ファイルには一切書かない（同期先のフォルダも作らない。台帳・手で消す写真の一覧も変えない）。
    ledger_dir を省くと default_ledger_dir()。中止したら cancelled（削除の数は数えない）。
    実際の同期では、JPEG を作れなかった写真・書けなかった同期先の分だけ数が減ることがある。
    """
    plan = FramePlan()
    targets = _clean_targets(targets)
    if not targets:
        plan.error = "同期先がありません（photo_ratings.json の frame_targets）"
        return plan
    ledger_dir = ledger_dir or default_ledger_dir()
    sig = dict(frame_settings(settings), format=FRAME_FORMAT)
    order, seen = _unique_sources(sources)
    plan.sources = order
    manual_keys = _manual_keys(manual_delete)
    states = [_Target(t, load_ledger(ledger_dir, t)) for t in targets]
    plan.targets = [FramePlanTarget(t.dir, manual=_key(t.dir) in manual_keys) for t in states]
    pairs = list(zip(states, plan.targets))
    scope_keys = None if scope is None else [_key(p) for p in scope]
    keep_keys = [_key(p) for p in keep]
    heif_cache: list = []
    total = len(order)
    for i, src in enumerate(order):
        if _cancelled(cancel):
            plan.cancelled = True
            return plan
        _notify(progress, i, total, os.path.basename(src))
        key = _key(src)
        try:
            st = os.stat(src)
        except OSError:
            plan.missing.append(src)
            continue
        edits = photo_edits(src)
        faces = None
        if (faces_provider is not None and frame_settings(settings).get("crop_aspect")
                and not (edits is not None and edits.crop is not None)):
            try:
                faces = faces_provider(src)
            except Exception:  # noqa: BLE001
                faces = None
        sig_i = _sig_with_edits(sig, edits)
        if faces:
            sig_i = dict(sig_i, faces=_faces_sig(faces))
        todo = []
        for t, out in pairs:
            action, name = _frame_action(t, key, st, sig_i)
            if action == "kept":
                out.kept.append(name)
            elif action == "unchanged":
                out.unchanged.append((name, src))
            else:
                todo.append((out, name, action))
        if not todo:
            continue
        skip = _cannot_build(src, heif_cache)
        if skip is not None:
            (plan.skipped_raw if skip == "raw" else plan.skipped_heic).append(src)
            continue
        for out, name, action in todo:
            if action == "update":
                out.updated.append((name, src))
            else:
                out.added.append(src)
    if _cancelled(cancel):
        plan.cancelled = True
        return plan
    _notify(progress, total, total, "対象から外れた写真を数えています")
    for t, out in pairs:
        for name, rec, action in _stale_entries(t, seen, scope_keys, keep_keys):
            if action == "delete":
                out.deleted.append((name, rec["source"]))
            elif action == "kept":
                out.kept.append(name)
    return plan


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
