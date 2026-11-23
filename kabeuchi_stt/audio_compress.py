# -*- coding: utf-8 -*-
"""録音 WAV の Opus 圧縮（GUI/Qt/COM 非依存）。

WAV は「あとで聞き直せればよい保険」なので、清書が終わったあと Opus（ogg・モノラル・約24kbps）へ変換する。
エンコード/デコードは faster-whisper の依存の PyAV（import av。libopus 入り）だけで行う（ffmpeg 不要）。
変換に失敗しても WAV は必ず残し、例外は呼び出し側へ出さずログに書く。
"""
from __future__ import annotations

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger("kabeuchi")

SAMPLE_RATE = 16000
OPUS_EXT = ".opus"
MODES = ("off", "keep", "trash", "delete")   # off=変換しない / keep=WAVを残す / trash=ごみ箱へ / delete=完全に削除
DEFAULT_MODE = "delete"
DEFAULT_KBPS = 24
MODE_LABELS = {
    "off": "圧縮しない（WAVのまま）",
    "keep": "Opus に圧縮して、元の WAV も残す",
    "trash": "Opus に圧縮して、元の WAV はごみ箱へ",
    "delete": "Opus に圧縮して、元の WAV は削除",
}


def normalize_mode(v) -> str:
    v = str(v or "").strip().lower()
    return v if v in MODES else DEFAULT_MODE


def kbps_of(settings: dict) -> int:
    try:
        k = int(settings.get("audio_compress_kbps", DEFAULT_KBPS))
    except (TypeError, ValueError):
        k = DEFAULT_KBPS
    return min(256, max(6, k))


def is_wav(path) -> bool:
    return Path(path).suffix.lower() == ".wav"


def _audio_seconds_wav(path: Path) -> float:
    from transcriber import load_wav_float
    return len(load_wav_float(path)) / SAMPLE_RATE


def encode_opus(src_wav: Path, dst: Path, kbps: int = DEFAULT_KBPS, audio: np.ndarray | None = None) -> Path:
    """WAV を Opus（ogg・モノラル）へ。dst は一時名で書いて最後に置き換える。失敗は例外。
    audio（float32 mono 16kHz）を渡すと、WAV を読まずそれを符号化する（無音を縮めたあとの音声用）。"""
    import av
    if audio is None:
        from transcriber import load_wav_float
        audio = load_wav_float(Path(src_wav))
    if len(audio) == 0:
        raise ValueError("音声が空です（長さ0）")
    pcm = np.clip(audio * 32768.0, -32768, 32767).astype(np.int16)
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        c = av.open(str(tmp), "w", format="ogg")
        try:
            s = c.add_stream("libopus", rate=SAMPLE_RATE)
            s.layout = "mono"
            s.bit_rate = int(kbps) * 1000
            rs = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE,
                                   frame_size=s.codec_context.frame_size or 320)

            def push(frame) -> None:
                for o in rs.resample(frame):
                    for p in s.encode(o):
                        c.mux(p)
            step = SAMPLE_RATE * 10
            for i in range(0, len(pcm), step):
                f = av.AudioFrame.from_ndarray(pcm[i:i + step].reshape(1, -1), format="s16", layout="mono")
                f.sample_rate = SAMPLE_RATE
                push(f)
            push(None)
            for p in s.encode(None):
                c.mux(p)
        finally:
            c.close()
        tmp.replace(dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return dst


def decode_audio_float(path, sr: int = SAMPLE_RATE) -> np.ndarray:
    """PyAV で任意の音声ファイル（Opus 等）を sr の mono float32 へ。"""
    import av
    out = []
    c = av.open(str(path))
    try:
        st = c.streams.audio[0]
        rs = av.AudioResampler(format="flt", layout="mono", rate=sr)
        for fr in c.decode(st):
            for o in rs.resample(fr):
                out.append(o.to_ndarray().reshape(-1))
        for o in rs.resample(None):
            out.append(o.to_ndarray().reshape(-1))
    finally:
        c.close()
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, dtype=np.float32)


# ---------------------------------------------------------------- 無音カット（Opus にするとき）
# 4秒以上続く明らかな無音を 0.5 秒に縮める。縮めた場所は cuts = [[start, end, keep], ...]（元の時刻・秒）で記録し、
# 履歴の再生位置の変換（元の時刻 <-> 縮めた音声の時刻）に使う。区間 [start,end] のうち、先頭 keep/2 と末尾 keep/2 だけを残す。
CUT_MIN_SEC = 4.0          # この長さ以上続く無音だけ縮める
CUT_KEEP_SEC = 0.5         # 縮めたあとの長さ（前後に半分ずつ残す）
CUT_FRAME_SEC = 0.1        # 無音判定のフレーム
CUT_ABS_DBFS = -60.0       # フレームのピークがこれ未満なら無音候補
CUT_FLOOR_MARGIN_DB = 12.0  # かつ、録音全体の床（フレームのピークの下位 10%）+ これ未満
CUT_PROTECT_PAD_SEC = 0.3  # 発話の区間の前後に、さらにこれだけ余白を守る
CUT_MIN_PEAK_DBFS = -50.0  # 録音全体のピークがこれより小さい（声が拾えていない録音）なら削らない（判定が怪しい）


def _dbfs(x: float) -> float:
    return 20.0 * float(np.log10(max(float(x), 1e-9)))


def find_silence_cuts(audio: np.ndarray, protect=(), sr: int = SAMPLE_RATE, min_sec: float = CUT_MIN_SEC,
                      keep_sec: float = CUT_KEEP_SEC, abs_dbfs: float = CUT_ABS_DBFS) -> list:
    """縮める無音の区間 [[start, end, keep], ...]（元の時刻・秒・時刻順）を返す。判定が怪しいときは空（削らない）。
    protect: 守る区間 [(start, end), ...]（発話の区間。前後に CUT_PROTECT_PAD_SEC の余白を足して、無音から除く）。"""
    a = np.abs(np.asarray(audio, dtype=np.float32))
    fr = max(1, int(round(CUT_FRAME_SEC * sr)))
    n = len(a) // fr
    if n < int((min_sec + 1.0) / CUT_FRAME_SEC):
        return []
    peaks = a[:n * fr].reshape(n, fr).max(axis=1)
    if _dbfs(a.max()) < CUT_MIN_PEAK_DBFS:      # 声が拾えていない録音は、無音と声を区別できない
        return []
    db = 20.0 * np.log10(np.maximum(peaks, 1e-9))
    floor = float(np.percentile(db, 10))
    silent = db < min(abs_dbfs, floor + CUT_FLOOR_MARGIN_DB)
    for seg in protect or ():
        try:
            s0, s1 = float(seg[0]) - CUT_PROTECT_PAD_SEC, float(seg[1]) + CUT_PROTECT_PAD_SEC
        except (TypeError, ValueError, IndexError):
            continue
        i0, i1 = max(0, int(np.floor(s0 / CUT_FRAME_SEC))), min(n, int(np.ceil(s1 / CUT_FRAME_SEC)))
        if i1 > i0:
            silent[i0:i1] = False
    cuts, i = [], 0
    need = int(round(min_sec / CUT_FRAME_SEC))
    while i < n:
        if not silent[i]:
            i += 1
            continue
        j = i
        while j < n and silent[j]:
            j += 1
        if j - i >= need:
            cuts.append([round(i * CUT_FRAME_SEC, 3), round(j * CUT_FRAME_SEC, 3), float(keep_sec)])
        i = j
    return cuts


def apply_cuts(audio: np.ndarray, cuts, sr: int = SAMPLE_RATE) -> np.ndarray:
    """cuts のとおりに音声を縮める（各区間は先頭 keep/2 と末尾 keep/2 を残す）。"""
    if not cuts:
        return audio
    out, pos = [], 0
    for s, e, k in cuts:
        a0, a1 = int(round(s * sr)), int(round(e * sr))
        h = int(round(k / 2.0 * sr))
        a0, a1 = min(a0, len(audio)), min(a1, len(audio))
        if a1 - a0 <= 2 * h:
            continue
        out.append(audio[pos:a0 + h])
        pos = a1 - h
    out.append(audio[pos:])
    return np.concatenate(out) if out else audio


def removed_seconds(cuts) -> float:
    return sum(max(0.0, float(e) - float(s) - float(k)) for s, e, k in (cuts or ()))


def orig_to_cut_time(t: float, cuts) -> float:
    """元の時刻 -> 縮めた音声上の時刻。縮めた区間の中は、残した部分（先頭/末尾の半分ずつ）の端に寄せる。"""
    t = float(t)
    shift = 0.0
    for s, e, k in cuts or ():
        s, e, k = float(s), float(e), float(k)
        d = e - s - k
        if d <= 0:
            continue
        if t <= s + k / 2.0:
            break
        if t < e - k / 2.0:
            return (s - shift) + k / 2.0
        shift += d
    return max(0.0, t - shift)


def cut_to_orig_time(t: float, cuts) -> float:
    """縮めた音声上の時刻 -> 元の時刻（再生位置から、行のハイライトを求める用）。"""
    t = float(t)
    shift = 0.0
    for s, e, k in cuts or ():
        s, e, k = float(s), float(e), float(k)
        d = e - s - k
        if d <= 0:
            continue
        c0 = s - shift                    # 縮めた音声での区間の開始
        if t <= c0 + k / 2.0:
            break
        shift += d
    return max(0.0, t + shift)


def cuts_to_json(cuts) -> str:
    import json
    return json.dumps([[round(float(s), 3), round(float(e), 3), round(float(k), 3)] for s, e, k in (cuts or ())])


def cuts_from_json(s) -> list:
    import json
    try:
        v = json.loads(s) if s else []
        return [[float(a), float(b), float(c)] for a, b, c in v]
    except Exception:
        return []


def opus_path_for(wav: Path) -> Path:
    return Path(wav).with_suffix(OPUS_EXT)


def compress_after_final(wav: Path, settings: dict,
                         trasher: Callable[[Path], None] | None = None,
                         protect=None, cuts_out: list | None = None) -> Path | None:
    """清書が済んだ WAV を設定どおり圧縮して後始末する。変換できたら Opus のパス、しなかった/失敗なら None。
    設定 audio_silence_cut（既定 True）なら、4秒以上の無音を 0.5 秒に縮めてから変換する。protect は守る区間
    [(start,end)]（発話）。縮めた場所は cuts_out（リスト）へ [[start,end,keep],...] で足す（履歴に保存する用）。

    決して例外を出さない。失敗・ファイルなし・長さが大きくずれる場合は WAV をそのまま残す。
    """
    try:
        mode = normalize_mode(settings.get("audio_compress", DEFAULT_MODE))
        wav = Path(wav)
        if mode == "off" or not is_wav(wav) or not wav.exists():
            return None
        dst = opus_path_for(wav)
        if dst.exists():   # 既存の別ファイルを上書きしない
            dst = wav.with_name(f"{wav.stem}_{uuid.uuid4().hex[:6]}{OPUS_EXT}")
        from transcriber import load_wav_float
        audio = load_wav_float(wav)
        cuts: list = []
        if settings.get("audio_silence_cut", True):
            try:
                cuts = find_silence_cuts(audio, protect or ())
            except Exception as e:      # 判定の失敗は、削らず変換だけ行う
                log.error("無音の判定に失敗（削らずに変換します）: %s", e)
                cuts = []
        encode_opus(wav, dst, kbps_of(settings), audio=apply_cuts(audio, cuts) if cuts else audio)
        if not dst.exists() or dst.stat().st_size <= 0:
            raise RuntimeError("Opus ファイルができませんでした")
        a = len(audio) / SAMPLE_RATE - removed_seconds(cuts)
        b = len(decode_audio_float(dst)) / SAMPLE_RATE
        if abs(a - b) > max(1.0, a * 0.05):
            raise RuntimeError(f"変換後の長さが合いません（期待 {a:.1f}秒 / 変換後 {b:.1f}秒）")
        if cuts_out is not None:
            cuts_out.extend(cuts)
    except Exception as e:
        log.error("Opus 圧縮に失敗（WAV は残します）: %s: %s", wav, e)
        try:
            for p in (locals().get("dst"),):
                if p is not None and Path(p).exists() and Path(p) != wav:
                    Path(p).unlink()
        except Exception:
            pass
        return None
    # ここから先は変換成功。元 WAV の後始末（失敗しても Opus は有効）
    try:
        if mode == "trash":
            if trasher is None:
                from history import send_to_recycle_bin as trasher
            trasher(wav)
        elif mode == "delete":
            wav.unlink()
    except Exception as e:
        log.error("元の WAV の後始末に失敗（WAV は残ります）: %s: %s", wav, e)
    return dst


def playback_source(path, cache_dir: Path | None = None) -> str:
    """QMediaPlayer に渡すパス。WAV はそのまま。Opus 等は PyAV で WAV に展開した一時ファイルを返す（失敗は元のパス）。"""
    if not path:
        return ""
    p = Path(path)
    if is_wav(p):
        return str(p)
    try:
        import wave
        d = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / "kabeuchi_stt_playback"
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.wav"):   # 古い展開物を掃除（再生中のものは消せないので無視）
            try:
                old.unlink()
            except OSError:
                pass
        x = decode_audio_float(p)
        out = d / f"{p.stem}_{uuid.uuid4().hex[:6]}.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(np.clip(x * 32768.0, -32768, 32767).astype("<i2").tobytes())
        return str(out)
    except Exception as e:
        log.error("再生用の展開に失敗: %s: %s", p, e)
        return str(p)
