"""以前の「受信ごとのフォルダ」（2026-10-17_130544）を、日付フォルダ（2026-10-17）にまとめる。

    python tools/merge_legacy_folders.py            # 何をするかを表示するだけ（変更しない）
    python tools/merge_legacy_folders.py --apply    # 実際に移動する

- 移動先に中身が同じファイルが既にあれば、移動せずに元を消す（同じ日の重複は持たない方針に合わせる）
- 名前がぶつかったら `name (1).jpg` のように番号を付ける（上書きはしない）
- `_heic` の中身は移動先の `_heic` へ
- 空になったフォルダは消す。それ以外のもの（想定外のファイルなど）が残ったら消さずに報告する
- 保存先は config.toml（LANDROP_CONFIG）の save_dir
"""

from __future__ import annotations

import filecmp
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from landrop.config import load_settings  # noqa: E402
from landrop.storage import SessionDirs, move_unique  # noqa: E402


def same_file_in(folder: Path, src: Path) -> Path | None:
    if not folder.is_dir():
        return None
    size = src.stat().st_size
    for p in folder.iterdir():
        if p.is_file() and p.stat().st_size == size and filecmp.cmp(p, src, shallow=False):
            return p
    return None


def plan(save_dir: Path):
    for d in sorted(save_dir.iterdir()):
        if not d.is_dir():
            continue
        try:
            day = datetime.strptime(d.name, SessionDirs.LEGACY_FORMAT).strftime(SessionDirs.DATE_FORMAT)
        except ValueError:
            continue
        target = save_dir / day
        for sub in ("", "_heic"):
            src_dir = d / sub if sub else d
            if not src_dir.is_dir():
                continue
            for f in sorted(src_dir.iterdir()):
                if f.is_file():
                    yield d, f, target / sub if sub else target


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    save_dir = load_settings().save_dir
    print(f"保存先: {save_dir}{'' if apply else '（確認のみ。--apply で実行）'}")
    moved = dup = 0
    legacy_dirs: set[Path] = set()
    for legacy, f, dest_dir in plan(save_dir):
        legacy_dirs.add(legacy)
        existing = same_file_in(dest_dir, f)
        rel = f.relative_to(save_dir)
        if existing:
            dup += 1
            print(f"  重複のため削除  {rel}  （= {existing.relative_to(save_dir)}）")
            if apply:
                f.unlink()
        else:
            moved += 1
            print(f"  移動            {rel}  → {dest_dir.relative_to(save_dir)}\\")
            if apply:
                move_unique(f, dest_dir, f.name)
    left = []
    for d in sorted(legacy_dirs):
        if apply:
            heic = d / "_heic"
            if heic.is_dir() and not any(heic.iterdir()):
                heic.rmdir()
            if any(d.iterdir()):
                left.append(d)
            else:
                d.rmdir()
    print(f"移動 {moved} 件 / 重複で削除 {dup} 件 / 対象フォルダ {len(legacy_dirs)} 個")
    for d in left:
        print(f"  消さずに残したフォルダ（中身が残っています）: {d}")


if __name__ == "__main__":
    main()
