# -*- coding: utf-8 -*-
"""
מודול ייצוא אודיו נייד (Portable Audiobook Exporter) עבור PorMyCodex.
מאפשר לייצר קובץ שמע אמיתי (.mp3 או .wav) מכל פרק, טקסט מתורגם או תסריט רב-קולי,
כדי להעביר לנגן, לוואטסאפ, לטלפון, לרכב או לכל מחשב אחר — ללא צורך ב-FFmpeg חיצוני.
"""

import os
import wave
import asyncio
import tempfile
from typing import List, Optional, Callable, Tuple


def _strip_id3v2_header(mp3_bytes: bytes) -> bytes:
    """מסיר תגית ID3v2 בתחילת מקטע MP3 משני כדי לאחד מספר מקטעי Edge-TTS לקובץ MP3 תקין אחד."""
    if len(mp3_bytes) >= 10 and mp3_bytes[:3] == b"ID3":
        # Synchsafe integer size in bytes 6..9
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
) -> str:
    """מייצא קובץ MP3 איכותי באמצעות Edge-TTS (כולל תמיכה בריבוי דוברים)."""
    import edge_tts

    segments = _group_segments(sentences, default_voice, voice_overrides)
    if not segments:
        raise ValueError("אין טקסט לייצוא.")

    rate_str = f"{int((speed - 1.0) * 100):+d}%"
    total = len(segments)

    async def synthesize_all() -> bytes:
        combined = bytearray()
        for i, (v_id, text_chunk) in enumerate(segments, 1):
            if progress_cb:
                progress_cb(f"מייצר אודיו MP3: חלק {i} מתוך {total}...")
            voice_name = _voice_id_to_edge_name(v_id)
            fd, tmp_part = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            try:
                communicate = edge_tts.Communicate(text_chunk, voice_name, rate=rate_str)
                await communicate.save(tmp_part)
                with open(tmp_part, "rb") as f:
                    part_bytes = f.read()
                if not part_bytes:
                    raise RuntimeError(f"התקבל מקטע ריק בחלק {i}")
                if i == 1:
                    combined.extend(part_bytes)
                else:
                    combined.extend(_strip_id3v2_header(part_bytes))
            finally:
                try:
                    if os.path.exists(tmp_part):
                        os.remove(tmp_part)
                except Exception:
                    pass
        return bytes(combined)

    mp3_data = asyncio.run(synthesize_all())
    if not mp3_data:
        raise RuntimeError("יצירת קובץ ה-MP3 נכשלה (תוכן ריק).")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "wb") as out_f:
        out_f.write(mp3_data)
    return output_path


def export_to_wav_offline(
    sentences: List[str],
    output_path: str,
    speed: float = 1.0,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """מייצא קובץ WAV אופליין מלא באמצעות מנוע הקול של Windows (SAPI5 / pyttsx3)."""
    import pyttsx3

    segments = _group_segments(sentences, "sapi5", None, max_chars_per_batch=3000)
    if not segments:
        raise ValueError("אין טקסט לייצוא.")

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

        total = len(segments)
        for i, (_, text_chunk) in enumerate(segments, 1):
            if progress_cb:
                progress_cb(f"מייצר אודיו WAV אופליין: חלק {i} מתוך {total}...")
            fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            temp_wavs.append(tmp_wav)
            engine.save_to_file(text_chunk, tmp_wav)
        engine.runAndWait()

        # איחוד כל קבצי ה-WAV לקובץ אחד תקין
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        params = None
        frames_all = []
        for tw in temp_wavs:
            if os.path.exists(tw) and os.path.getsize(tw) > 44:
                with wave.open(tw, "rb") as w:
                    if params is None:
                        params = w.getparams()
                    frames_all.append(w.readframes(w.getnframes()))

        if not params or not frames_all:
            raise RuntimeError("מנוע SAPI5 לא יצר נתוני שמע. ודא שמותקן קול דיבור ב-Windows.")

        with wave.open(output_path, "wb") as out_w:
            out_w.setparams(params)
            for fr in frames_all:
                out_w.writeframes(fr)

        return output_path
    finally:
        for tw in temp_wavs:
            try:
                if os.path.exists(tw):
                    os.remove(tw)
            except Exception:
                pass


def export_audiobook_file(
    sentences: List[str],
    output_path: str,
    default_voice: str = "edge_avri",
    speed: float = 1.0,
    voice_overrides: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """מזהה לפי סיומת הקובץ (.mp3 או .wav) ומייצא קובץ שמע נייד לשיתוף."""
    ext = os.path.splitext(output_path)[1].lower()
    if ext == ".wav" or default_voice == "sapi5" and ext != ".mp3":
        if ext != ".wav":
            output_path = f"{output_path}.wav"
        return export_to_wav_offline(sentences, output_path, speed=speed, progress_cb=progress_cb)
    else:
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
            )
        except Exception as e:
            # אם Edge-TTS נכשל בגלל חוסר אינטרנט, נופל אוטומטית לייצוא WAV אופליין
            wav_fallback = os.path.splitext(output_path)[0] + ".wav"
            if progress_cb:
                progress_cb(f"Edge-TTS אינו זמין ({e}), עובר לייצוא WAV מקומי...")
            return export_to_wav_offline(sentences, wav_fallback, speed=speed, progress_cb=progress_cb)
