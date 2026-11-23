# -*- coding: utf-8 -*-
"""清書ジョブの実体（GUI/Qt/COM 非依存）。

WAV を清書 → 置換辞書を適用 → txt/md 保存 → 履歴へ登録（やり直しなら更新）→ タイムライン反映。
FinalizeQueue の run_job として使う。
"""
from __future__ import annotations

from typing import Callable

from finalize_queue import Job
from audio_compress import compress_after_final
from history import History
from paragraph import format_for_output, plain_text
from transcriber import transcribe_wav


def make_run_job(get_settings: Callable[[], dict], replacer, history: History, cache,
                 transcribe: Callable = transcribe_wav,
                 on_status: Callable[[Job, str], None] = lambda j, m: None):
    def compress_and_record(job: Job, s: dict, segments) -> None:
        """WAV を設定どおり Opus へ圧縮（無音カット込み）し、履歴の音声パスと縮めた場所を更新する。失敗しても WAV を残す"""
        try:
            cuts: list = []
            protect = [(x[0], x[1]) for x in (segments or ()) if str(x[2]).strip()]
            opus = compress_after_final(job.wav, s, protect=protect, cuts_out=cuts)
            if opus is not None:
                history.set_audio_path(job.history_id, opus)
                history.set_cuts(job.history_id, cuts)
        except Exception:
            pass

    def run_job(job: Job):
        s = get_settings()
        if job.compress_only:       # 清書しない設定: 圧縮（と無音カット）だけ。履歴は登録済み
            rec = history.get(job.history_id) if job.history_id else None
            if rec is not None:
                compress_and_record(job, s, rec.segments)
            return None
        name = job.model or s.get("final_model", "small")
        threads = int(s.get("final_cpu_threads", 0))

        def prog(p: float) -> None:
            job.progress = p

        def status(m: str) -> None:
            job.message = m
            on_status(job, m)

        extra = {"file_tag": job.file_tag} if job.file_tag else {}
        with cache.use(name, threads) as model:
            r = transcribe(job.wav, s, model_name=name, on_progress=prog, on_status=status,
                           cancel=job.cancel, model=model, postprocess=replacer.apply,
                           formatter=lambda segs, gaps=None: format_for_output(segs, s, gaps), **extra)
        if job.cancel.is_set():
            return None  # 中断。txt/md は途中まで保存されるが履歴には載せない
        # 履歴には元の区間（時刻つき・無加工）と、整形しない本文を残す。整形は出力（txt/md/クリップボード）だけ
        raw = plain_text(r.segments)
        if job.history_id:
            history.update_final(job.history_id, text=raw, segments=r.segments,
                                 duration=r.audio_seconds, model=name,
                                 txt_path=r.txt_path or "", md_path=r.md_path or "")
        else:
            job.history_id = history.add(text=raw, segments=r.segments, duration=r.audio_seconds,
                                         model=name, wav_path=job.wav, txt_path=r.txt_path or "",
                                         md_path=r.md_path or "")
        if job.compress and job.history_id:
            # 清書が WAV を読み終え履歴に載ってから圧縮する（清書の入力を消さない）。失敗しても WAV を残して続行
            compress_and_record(job, s, r.segments)
        if job.timeline is not None:
            job.timeline.set_final(r.segments, r.audio_seconds, gaps=getattr(r, "gaps", None))
        return r
    return run_job
