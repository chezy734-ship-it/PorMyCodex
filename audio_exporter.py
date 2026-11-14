# -*- coding: utf-8 -*-
"""
מודול ייצוא אודיו נייד (Portable Audiobook Exporter) עבור PorMyCodex.
מאפשר לייצר קובץ שמע אמיתי (.mp3 או .wav) מכתיבה אטומית מלאה (temp + fsync + os.replace):
- אימות של 100% מהמקטעים (לעולם לא מדלג על מקטע שנכשל ולא יוצר קובץ חלקי)
- תמיכה מלאה בביטול נקי (cancel_event) בלי להשאיר שאריות בנתיב היעד
"""

import asyncio
import io
import os
import tempfile
import wave
from typing import Any, Callable, List, Optional, Tuple

from library_store import atomic_write_bytes


class ExportCancelledError(RuntimeError):
    """נזרק כאשר פעולת ייצוא האודיו בוטלה במפורש."""


def _check_cancelled(cancel_event: Any = None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelledError("ייצוא השמע בוטל על ידי המשתמש.")


def _strip_id3v2_header(mp3_bytes: bytes) -> bytes:
    """מסיר תגית ID3v2 בתחילת מקטע MP3 משני כדי לאחד מספר מקטעי Edge-TTS לקובץ MP3 תקין אחד."""
    if len(mp3_bytes) >= 10 and mp3_bytes[:3] == b"ID3":
        size = (
            ((mp3_bytes[6] & 0x7F) << 21)
            | ((mp3_bytes[7] & 0x7F) << 14)
            | ((mp3_bytes[8] & 0x7F) << 7)
            | (mp3_bytes[9] & 0x7F)
        )
        offset = 10 + size
        if offset < len(mp3_bytes):
            return mp3_bytes[offset:]
    return mp3_bytes


def _group_segments(
    sentences: List[str],
    default_voice: str,
    voice_overrides: Optional[List[str]] = None,
    max_chars_per_batch: int = 1600,
) -> List[Tuple[str, str]]:
    """
    מאחד משפטים רצופים של אותו דובר למקטעי הקראה אופטימליים.
    בתסריט מורה-תלמיד כל החלפת דובר פותחת מקטע עם הקול המתאים.
    """
    segments: List[Tuple[str, str]] = []
    cur_voice = ""
    cur_buf: List[str] = []
    cur_len = 0

    for idx, sent in enumerate(sentences):
        clean = (sent or "").strip()
        if not clean:
            continue
        v = default_voice
        if voice_overrides and idx < len(voice_overrides) and voice_overrides[idx]:
            v = voice_overrides[idx]

        if cur_voice and (v != cur_voice or cur_len + len(clean) + 1 > max_chars_per_batch):
            segments.append((cur_voice, " ".join(cur_buf)))
            cur_voice = v
            cur_buf = [clean]
            cur_len = len(clean)
        else:
            cur_voice = v
            cur_buf.append(clean)
            cur_len += len(clean) + 1

    if cur_buf:
        segments.append((cur_voice, " ".join(cur_buf)))

    return segments


def _voice_id_to_edge_name(voice_id: str) -> str:
    if "hila" in (voice_id or "").lower():
        return "he-IL-HilaNeural"
    return "he-IL-AvriNeural"


def export_to_mp3(
    sentences: List[str],
    output_path: str,
    default_voice: str = "edge_avri",
    speed: float = 1.0,
    voice_overrides: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_event: Any = None,
    mp3_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
) -> str:
    """מייצא קובץ MP3 איכותי באמצעות Edge-TTS (או מסנתז מוזרק לבדיקות) בכתיבה אטומית מלאה."""
    _check_cancelled(cancel_event)

    segments = _group_segments(sentences, default_voice, voice_overrides)
    if not segments:
        raise ValueError("אין טקסט לייצוא.")

    rate_str = f"{int((speed - 1.0) * 100):+d}%"
    total = len(segments)
    combined = bytearray()

    if mp3_synth_fn is not None:
        for i, (v_id, text_chunk) in enumerate(segments, 1):
            _check_cancelled(cancel_event)
            if progress_cb:
                progress_cb(f"מייצר אודיו MP3: חלק {i} מתוך {total}...")
            voice_name = _voice_id_to_edge_name(v_id)
            part_bytes = mp3_synth_fn(text_chunk, voice_name, speed)
            if not part_bytes:
                raise RuntimeError(f"התקבל מקטע MP3 ריק בחלק {i} מתוך {total}.")
            if i == 1:
                combined.extend(part_bytes)
            else:
                combined.extend(_strip_id3v2_header(part_bytes))
    else:
        import edge_tts

        async def synthesize_all() -> bytes:
            out_buf = bytearray()
            for i, (v_id, text_chunk) in enumerate(segments, 1):
                _check_cancelled(cancel_event)
                if progress_cb:
                    progress_cb(f"מייצר אודיו MP3: חלק {i} מתוך {total}...")
                voice_name = _voice_id_to_edge_name(v_id)
                fd, tmp_part = tempfile.mkstemp(suffix=".mp3")
                os.close(fd)
                try:
                    communicate = edge_tts.Communicate(text_chunk, voice_name, rate=rate_str)
                    await communicate.save(tmp_part)
                    _check_cancelled(cancel_event)
                    with open(tmp_part, "rb") as f:
                        part_bytes = f.read()
                    if not part_bytes:
                        raise RuntimeError(f"התקבל מקטע MP3 ריק בחלק {i} מתוך {total}.")
                    if i == 1:
                        out_buf.extend(part_bytes)
                    else:
                        out_buf.extend(_strip_id3v2_header(part_bytes))
                finally:
                    try:
                        if os.path.exists(tmp_part):
                            os.remove(tmp_part)
                    except Exception:
                        pass
            return bytes(out_buf)

        combined.extend(asyncio.run(synthesize_all()))

    _check_cancelled(cancel_event)
    mp3_data = bytes(combined)
    if not mp3_data:
        raise RuntimeError("יצירת קובץ ה-MP3 נכשלה (תוכן ריק).")

    return atomic_write_bytes(output_path, mp3_data)


def export_to_wav_offline(
    sentences: List[str],
    output_path: str,
    speed: float = 1.0,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_event: Any = None,
    wav_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    default_voice: str = "sapi5",
    voice_overrides: Optional[List[str]] = None,
) -> str:
    """מייצא קובץ WAV אופליין מלא בכתיבה אטומית, בלי לדלג על מקטעים שכשלו."""
    _check_cancelled(cancel_event)

    segments = _group_segments(sentences, default_voice, voice_overrides, max_chars_per_batch=3000)
    if not segments:
        raise ValueError("אין טקסט לייצוא.")

    total = len(segments)
    params = None
    frames_all: List[bytes] = []

    if wav_synth_fn is not None:
        for i, (v_id, text_chunk) in enumerate(segments, 1):
            _check_cancelled(cancel_event)
            if progress_cb:
                progress_cb(f"מייצר אודיו WAV: חלק {i} מתוך {total}...")
            wav_bytes = wav_synth_fn(text_chunk, v_id, speed)
            if not wav_bytes or len(wav_bytes) <= 44:
                raise RuntimeError(f"התקבל מקטע WAV ריק/לא תקין בחלק {i} מתוך {total}.")
            with wave.open(io.BytesIO(wav_bytes), "rb") as w:
                if params is None:
                    params = w.getparams()
                chunk_frames = w.readframes(w.getnframes())
                if not chunk_frames:
                    raise RuntimeError(f"לא התקבלו פריימים של שמע בחלק {i} מתוך {total}.")
                frames_all.append(chunk_frames)
    else:
        import pyttsx3

        temp_wavs: List[str] = []
        try:
            engine = pyttsx3.init()
            base_rate = engine.getProperty("rate")
            engine.setProperty("rate", int(base_rate * speed))
            voices = engine.getProperty("voices")
            for v in voices:
                if any(k in (v.name + " " + str(v.languages)).lower() for k in ("hebrew", "he-il", "asaf", "hila")):
                    engine.setProperty("voice", v.id)
                    break

            for i, (_, text_chunk) in enumerate(segments, 1):
                _check_cancelled(cancel_event)
                if progress_cb:
                    progress_cb(f"מייצר אודיו WAV אופליין: חלק {i} מתוך {total}...")
                fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                temp_wavs.append(tmp_wav)
                engine.save_to_file(text_chunk, tmp_wav)
            engine.runAndWait()
            _check_cancelled(cancel_event)

            for i, tw in enumerate(temp_wavs, 1):
                if not os.path.exists(tw) or os.path.getsize(tw) <= 44:
                    raise RuntimeError(f"מנוע SAPI5 נכשל ביצירת חלק {i} מתוך {total}.")
                with wave.open(tw, "rb") as w:
                    if params is None:
                        params = w.getparams()
                    chunk_frames = w.readframes(w.getnframes())
                    if not chunk_frames:
                        raise RuntimeError(f"מנוע SAPI5 הפיק שמע ריק בחלק {i} מתוך {total}.")
                    frames_all.append(chunk_frames)
        finally:
            for tw in temp_wavs:
                try:
                    if os.path.exists(tw):
                        os.remove(tw)
                except Exception:
                    pass

    _check_cancelled(cancel_event)
    if not params or len(frames_all) != total:
        raise RuntimeError("ייצוא ה-WAV נכשל: חסרים מקטעי שמע.")

    out_buf = io.BytesIO()
    with wave.open(out_buf, "wb") as out_w:
        out_w.setparams(params)
        for fr in frames_all:
            out_w.writeframes(fr)

    return atomic_write_bytes(output_path, out_buf.getvalue())


def export_audiobook_file(
    sentences: List[str],
    output_path: str,
    default_voice: str = "edge_avri",
    speed: float = 1.0,
    voice_overrides: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_event: Any = None,
    allow_fallback: bool = False,
    mp3_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    wav_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
) -> str:
    """
    מייצא קובץ שמע נייד (.mp3 או .wav) באופן אטומי.
    כברירת מחדל (allow_fallback=False) אינו מסתיר כשל ב-MP3 מאחורי פורמט חלופי.
    """
    _check_cancelled(cancel_event)

    ext = os.path.splitext(output_path)[1].lower()
    if ext == ".wav" or (default_voice == "sapi5" and ext != ".mp3"):
        if ext != ".wav":
            output_path = f"{output_path}.wav"
        return export_to_wav_offline(
            sentences,
            output_path,
            speed=speed,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            wav_synth_fn=wav_synth_fn,
            default_voice=default_voice,
            voice_overrides=voice_overrides,
        )

    if ext != ".mp3":
        output_path = f"{output_path}.mp3"
    try:
        return export_to_mp3(
            sentences,
            output_path,
            default_voice=default_voice,
            speed=speed,
            voice_overrides=voice_overrides,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            mp3_synth_fn=mp3_synth_fn,
        )
    except ExportCancelledError:
        raise
    except Exception as e:
        if not allow_fallback:
            raise
        wav_fallback = os.path.splitext(output_path)[0] + ".wav"
        if progress_cb:
            progress_cb(f"Edge-TTS אינו זמין ({e}), עובר לייצוא WAV מקומי...")
        return export_to_wav_offline(
            sentences,
            wav_fallback,
            speed=speed,
            progress_cb=progress_cb,
            cancel_event=cancel_event,
            wav_synth_fn=wav_synth_fn,
            default_voice=default_voice,
            voice_overrides=voice_overrides,
        )
