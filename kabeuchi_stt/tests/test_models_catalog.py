# -*- coding: utf-8 -*-
"""認識モデルのカタログ・取得済み判定・取得（偽物に差し替え。通信しない）・モデルごとの常駐キャッシュ。"""
import tempfile
import threading
import unittest
from pathlib import Path

import _common as C  # noqa: F401
import models_catalog as MC
from model_cache import ModelCache, find_local_model, repo_id_for


def put(model_dir: Path, rel: str) -> None:
    p = Path(model_dir) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


class CatalogTest(unittest.TestCase):
    def test_catalog_has_expected_entries(self):
        ids = [m.id for m in MC.CATALOG]
        self.assertEqual(ids, ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3", "kotoba-whisper"])
        k = MC.find("kotoba-whisper")
        self.assertEqual(k.model, "kotoba-tech/kotoba-whisper-v2.0-faster")
        self.assertIs(MC.find(k.model), k)
        self.assertEqual(MC.model_name("kotoba-whisper"), k.model)
        self.assertEqual(MC.model_name("unknown-x"), "unknown-x")          # カタログ外はそのまま

    def test_repo_ids(self):
        self.assertEqual(repo_id_for("large-v3-turbo"), "mobiuslabsgmbh/faster-whisper-large-v3-turbo")
        self.assertEqual(repo_id_for("small"), "Systran/faster-whisper-small")
        self.assertEqual(repo_id_for("a/b"), "a/b")

    def test_is_downloaded_rules(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.assertFalse(MC.is_downloaded("small", d))
            put(d, "small/model.bin")                                      # ① 手置き
            self.assertTrue(MC.is_downloaded("small", d))
            self.assertEqual(find_local_model("small", d)[0], "manual")
            put(d, "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/abc/model.bin")   # ② キャッシュ
            self.assertTrue(MC.is_downloaded("large-v3-turbo", d))
            self.assertEqual(find_local_model("large-v3-turbo", d)[0], "cache")
            self.assertFalse(MC.is_downloaded("large-v3", d))
            put(d, "kotoba-tech/kotoba-whisper-v2.0-faster/model.bin")     # リポジトリ ID の名前の手置き
            self.assertTrue(MC.is_downloaded("kotoba-whisper", d))
            self.assertTrue(MC.is_downloaded("kotoba-tech/kotoba-whisper-v2.0-faster", d))
            self.assertFalse(MC.is_downloaded("", d))

    def test_incomplete_cache_is_not_downloaded(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            put(d, "models--Systran--faster-whisper-base/snapshots/abc/config.json")   # model.bin が無い
            self.assertFalse(MC.is_downloaded("base", d))

    def test_labels_and_combo_items(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            put(d, "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/snapshots/abc/model.bin")
            lab = MC.display_label("large-v3-turbo", d)
            self.assertEqual(lab, "large-v3-turbo — 精度:高 速さ:中 約1.6GB【取得済み】")
            self.assertTrue(MC.display_label("large-v3", d).endswith("【未取得】"))
            items = MC.combo_items(d, "my/own-model")
            self.assertEqual(len(items), len(MC.CATALOG) + 1)             # カタログ外の現在値も選べる
            self.assertEqual(items[-1][1], "my/own-model")
            self.assertEqual(len(MC.combo_items(d, "kotoba-whisper")), len(MC.CATALOG))
            self.assertEqual(len(MC.combo_items(d, "")), len(MC.CATALOG))


class DownloadTest(unittest.TestCase):
    def test_success_reports_progress_and_creates_model(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            calls, prog = [], []

            def fake(repo, model_dir, cb, cancel):
                calls.append(repo)
                cb(10, 100)
                cb(100, 100)
                put(Path(model_dir), "models--Systran--faster-whisper-large-v3/snapshots/h/model.bin")
                return str(Path(model_dir) / "x")
            r = MC.download("large-v3", d, lambda a, b: prog.append((a, b)), threading.Event(), snapshot=fake)
            self.assertTrue(r.ok, r.message)
            self.assertEqual(calls, ["Systran/faster-whisper-large-v3"])
            self.assertEqual(prog, [(10, 100), (100, 100)])
            self.assertTrue(MC.is_downloaded("large-v3", d))

    def test_kotoba_uses_repo_id(self):
        with tempfile.TemporaryDirectory() as t:
            seen = []

            def fake(repo, model_dir, cb, cancel):
                seen.append(repo)
                put(Path(model_dir), "models--kotoba-tech--kotoba-whisper-v2.0-faster/snapshots/h/model.bin")
                return ""
            r = MC.download("kotoba-whisper", t, snapshot=fake)
            self.assertTrue(r.ok)
            self.assertEqual(seen, ["kotoba-tech/kotoba-whisper-v2.0-faster"])

    def test_failure_has_japanese_reason_and_manual_steps(self):
        with tempfile.TemporaryDirectory() as t:
            def fake(*a):
                raise OSError("SSL: CERTIFICATE_VERIFY_FAILED")
            r = MC.download("kotoba-whisper", t, snapshot=fake)
            self.assertFalse(r.ok)
            self.assertFalse(r.cancelled)
            self.assertIn("取得できませんでした", r.message)
            self.assertIn("SSL", r.message)
            self.assertIn("手順", r.message)                                # 手置きの手順への案内つき
            self.assertIn("kotoba-tech/kotoba-whisper-v2.0-faster", r.message)
            self.assertIn("model.bin", r.message)

    def test_cancel(self):
        with tempfile.TemporaryDirectory() as t:
            ev = threading.Event()

            def fake(repo, model_dir, cb, cancel):
                ev.set()
                raise MC.DownloadCancelled()
            r = MC.download("small", t, snapshot=fake, cancel=ev)
            self.assertFalse(r.ok)
            self.assertTrue(r.cancelled)

    def test_finished_without_model_bin_is_failure(self):
        with tempfile.TemporaryDirectory() as t:
            r = MC.download("small", t, snapshot=lambda *a: "")
            self.assertFalse(r.ok)


class CachePerModelTest(unittest.TestCase):
    def test_each_model_is_kept_separately_and_repo_id_names_work(self):
        loaded = []
        cache = ModelCache(lambda n, th: (loaded.append((n, th)), object())[1], idle_sec=0)
        kot = "kotoba-tech/kotoba-whisper-v2.0-faster"
        with cache.use("small", 0) as a:
            pass
        with cache.use(kot, 0) as b:
            pass
        with cache.use("small", 0) as a2:
            pass
        self.assertIs(a, a2)
        self.assertIsNot(a, b)
        self.assertEqual(loaded, [("small", 0), (kot, 0)])
        with cache.use("small", 4):                                       # スレッド数が違えば別
            pass
        self.assertEqual(cache.loads, 3)
        self.assertEqual(sorted(cache.loaded), sorted(["small", kot, "small"]))
        self.assertEqual(sorted(cache.release_idle()), sorted(["small", kot, "small"]))   # 使わなくなったものは解放される


class HistorySharedFilesTest(unittest.TestCase):
    def test_delete_keeps_files_used_by_another_record(self):
        from history import History
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as t:
            d = Path(t)
            wav = d / "a.wav"
            wav.write_bytes(b"w")
            txt1, txt2 = d / "a.txt", d / "a_x.txt"
            txt1.write_text("1")
            txt2.write_text("2")
            h = History(d / "h.db")
            i1 = h.add(text="あ。", segments=[], duration=1, model="small", wav_path=wav, txt_path=txt1)
            i2 = h.add(text="い。", segments=[], duration=1, model="turbo", wav_path=wav, txt_path=txt2)
            trashed = []
            h.delete(i1, trasher=lambda p: trashed.append(Path(p)))
            self.assertEqual(trashed, [txt1])                              # 共有の WAV は残す
            h.delete(i2, trasher=lambda p: trashed.append(Path(p)))
            self.assertIn(wav, trashed)                                    # 最後の1件を消すときは WAV も移す


if __name__ == "__main__":
    unittest.main()
