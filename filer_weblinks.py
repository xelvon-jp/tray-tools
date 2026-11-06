# filer_weblinks.py
# 同期フォルダ(SharePoint / OneDrive / Dropbox 等)の中のファイルに、Web の
# URL を割り当てる。あっふの C メニュー「Web リンクをコピー」が使う。
#
# 仕組みは単純で、settings.json の filer.web_link_bases に
# {name, root: ローカルの同期フォルダ, url_base: 対応する Web の場所} を
# 書いておくと、root の中にあるファイルの URL を url_base + 相対パスで
# 組み立てる。Box のように台帳と突き合わせはしない(同期フォルダの相対
# パス = Web のパス、という同期サービスの前提に乗る)。作った URL が
# 実際に開けるかは各サービス側の設定に依存する(未検証)。
#
# Teams / Slack への共有は、この URL を貼るだけなので、専用の連携は
# 持たない(貼れば相手にも開ける、がこの形の利点)。
import os
import urllib.parse


def normalize_web_link_bases(value) -> list:
    """settings の web_link_bases を [{name, root, url_base}] に整える。

    手で編集される値なので、root が無い・url_base が http(s) で始まらない
    要素は捨てる。"""
    if not isinstance(value, list):
        return []
    found = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        root = str(entry.get("root") or "").strip()
        url_base = str(entry.get("url_base") or "").strip().rstrip("/")
        if not root or not url_base.lower().startswith(("http://", "https://")):
            continue
        name = str(entry.get("name") or "").strip() or url_base
        found.append({"name": name, "root": os.path.normpath(root),
                      "url_base": url_base})
    return found


def _disp(path: str) -> str:
    """照合と表示に使う形。区切りを / にそろえる(Windows の \\ と混在しても当てる)。"""
    return os.path.normpath(str(path)).replace("\\", "/")


def web_link_for(path, bases):
    """path に対応する (名前, URL)。どの root の中でもなければ None。

    複数の root に当てはまるときは、いちばん長い(深い)root を優先する。
    大文字小文字は区別せずに照合し、URL に使う相対パスは元の綴りのまま
    (区切りごとに URL エンコードする)。"""
    if not path or not bases:
        return None
    target_disp = _disp(path)
    target_key = target_disp.casefold()
    best = None
    for base in bases:
        root_disp = _disp(base["root"])
        root_key = root_disp.casefold()
        if target_key == root_key:
            rel = ""
        elif target_key.startswith(root_key + "/"):
            rel = target_disp[len(root_disp) + 1:]
        else:
            continue
        if best is None or len(root_disp) > len(best[0]["root"]):
            best = (base, rel)
    if best is None:
        return None
    base, rel = best
    if not rel:
        return base["name"], base["url_base"]
    quoted = "/".join(urllib.parse.quote(seg, safe="")
                      for seg in rel.split("/"))
    return base["name"], base["url_base"] + "/" + quoted
