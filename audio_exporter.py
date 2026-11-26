# -*- coding: utf-8 -*-
"""
מודול ייצוא ואריזת אודיו נייד (Portable Audiobook Exporter) עבור PorMyCodex.

מאפיינים מרכזיים:
- הזרמת כתיבה לקובץ זמני על הדיסק בחתיכות (Chunked Streaming) בלי להחזיק ספר שלם בזיכרון
- איחוד WAV תקני דרך פענוח וקידוד מחדש ב-`wave` (לעולם לא חיבור בינארי עיוור)
- איחוד MP3 תקני דרך פענוח וקידוד מחדש (FFmpeg / מסנתז ייעודי); לעולם לא חיבור בינארי עיוור של קובצי MP3
- אריזת ספר שלם לתיקייה או ל-ZIP של קבצים ממוספרים לכל פרק + manifest.json מאומת
- כתיבה אטומית מלאה (Temp file -> fsync -> os.replace) וביטול מיידי בלי להשאיר קבצים חלקיים
"""

import asyncio
import io
import json
import os
import shutil
import subprocess
import tempfile
import wave
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from library_store import atomic_write_bytes, atomic_write_json, sanitize_filename_part


class ExportCancelledError(RuntimeError):
    """נזרק כאשר פעולת ייצוא האודיו בוטלה במפורש."""


def _check_cancelled(cancel_event: Any = None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ExportCancelledError("ייצוא השמע בוטל על ידי המשתמש.")


def find_ffmpeg_executable() -> Optional[str]:
    """מאתר ffmpeg זמין במערכת או דרך imageio_ffmpeg (ללא תלות חובה)."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        cand = imageio_ffmpeg.get_ffmpeg_exe()
        if cand and os.path.exists(cand):
            return cand
    except Exception:
        pass
    return None


def _group_segments(
    sentences: List[str],
    default_voice: str,
    voice_overrides: Optional[List[str]] = None,
    max_chars_per_batch: int = 1600,
) -> List[Tuple[str, str]]:
    """מאחד משפטים רצופים של אותו דובר למקטעי הקראה מוגדרים."""
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


def merge_wav_files_streaming(
    wav_paths: List[str],
    output_path: str,
    *,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> str:
    """
    מאחד רשימת קובצי WAV מסודרת לקובץ WAV תקין אחד באמצעות פענוח וכתיבה בהזרמה על הדיסק.
    מחזיק בזיכרון בלוק יחיד (64K פריימים) בכל רגע — מתאים לספרים ארוכים מאוד.
    """
    _check_cancelled(cancel_event)
    if not wav_paths:
        raise ValueError("אין קובצי WAV לאיחוד.")

    abs_target = os.path.abspath(output_path)
    parent = os.path.dirname(abs_target)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_out = tempfile.mkstemp(dir=parent, prefix=f".tmp_{os.path.basename(abs_target)}_", suffix=".wav.part")
    os.close(fd)

    expected_spec = None
    total = len(wav_paths)

    try:
        with wave.open(tmp_out, "wb") as out_w:
            for idx, wp in enumerate(wav_paths, 1):
                _check_cancelled(cancel_event)
                if not wp or not os.path.exists(wp) or os.path.getsize(wp) <= 44:
                    raise RuntimeError(f"איחוד WAV נכשל: קובץ פרק {idx} חסר או ריק ({wp}).")
                if progress_cb:
                    progress_cb(f"מאחד שמע WAV תקני: קובץ {idx} מתוך {total}...")

                with wave.open(wp, "rb") as in_w:
                    spec = (in_w.getnchannels(), in_w.getsampwidth(), in_w.getframerate())
                    if expected_spec is None:
                        expected_spec = spec
                        out_w.setnchannels(spec[0])
                        out_w.setsampwidth(spec[1])
                        out_w.setframerate(spec[2])
                    elif spec != expected_spec:
                        raise RuntimeError(
                            f"פורמט השמע בקובץ {idx} ({spec}) אינו תואם לפורמט הקודם ({expected_spec})."
                        )

                    nframes = in_w.getnframes()
                    if nframes <= 0:
                        raise RuntimeError(f"קובץ WAV {idx} אינו מכיל פריימים של שמע.")
                    while True:
                        _check_cancelled(cancel_event)
                        chunk = in_w.readframes(16384)
                        if not chunk:
                            break
                        out_w.writeframes(chunk)

        _check_cancelled(cancel_event)
        with open(tmp_out, "rb+") as f_sync:
            f_sync.flush()
            os.fsync(f_sync.fileno())
        os.replace(tmp_out, abs_target)
        return abs_target
    except Exception:
        try:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
        except Exception:
            pass
        raise


def merge_mp3_files_decoded(
    mp3_paths: List[str],
    output_path: str,
    *,
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    mp3_merge_fn: Optional[Callable[[List[str], str], str]] = None,
) -> str:
    """
    מאחד קובצי MP3 רק באמצעות פענוח וקידוד מחדש אמיתיים (FFmpeg או פונקציית מיזוג ייעודית).
    אוסר חיבור בינארי ישיר של קבצים!
    """
    _check_cancelled(cancel_event)
    if not mp3_paths:
        raise ValueError("אין קובצי MP3 לאיחוד.")

    for idx, p in enumerate(mp3_paths, 1):
        if not p or not os.path.exists(p) or os.path.getsize(p) <= 0:
            raise RuntimeError(f"איחוד MP3 נכשל: קובץ {idx} חסר או ריק.")

    abs_target = os.path.abspath(output_path)
    parent = os.path.dirname(abs_target)
    os.makedirs(parent, exist_ok=True)

    if len(mp3_paths) == 1:
        fd, tmp_out = tempfile.mkstemp(dir=parent, prefix=f".tmp_{os.path.basename(abs_target)}_", suffix=".mp3.part")
        os.close(fd)
        try:
            with open(mp3_paths[0], "rb") as src_f, open(tmp_out, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f, length=65536)
                dst_f.flush()
                os.fsync(dst_f.fileno())
            _check_cancelled(cancel_event)
            os.replace(tmp_out, abs_target)
            return abs_target
        except Exception:
            if os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except Exception:
                    pass
            raise

    if mp3_merge_fn is not None:
        fd, tmp_out = tempfile.mkstemp(dir=parent, prefix=f".tmp_{os.path.basename(abs_target)}_", suffix=".mp3")
        os.close(fd)
        try:
            if progress_cb:
                progress_cb(f"מפענח ומאחד {len(mp3_paths)} קובצי MP3...")
            produced = mp3_merge_fn(list(mp3_paths), tmp_out)
            _check_cancelled(cancel_event)
            if not produced or not os.path.exists(produced) or os.path.getsize(produced) <= 0:
                raise RuntimeError("מנוע איחוד ה-MP3 יצר קובץ ריק.")
            os.replace(produced, abs_target)
            return abs_target
        finally:
            if os.path.exists(tmp_out):
                try:
                    os.remove(tmp_out)
                except Exception:
                    pass

    ffmpeg_exe = find_ffmpeg_executable()
    if not ffmpeg_exe:
        raise RuntimeError(
            "איחוד מספר קובצי MP3 לקובץ יחיד דורש פענוח וקידוד מחדש עם FFmpeg (איננו מבצעים חיבור בינארי לא תקני).\n"
            "בחר באחת האפשרויות הבאות: 1) ייצוא 'ZIP / תיקייה של קובצי MP3 לכל פרק + manifest', 2) 'קובץ WAV מאוחד לכל הספר', או התקן FFmpeg."
        )

    fd_list, concat_list_path = tempfile.mkstemp(dir=parent, prefix=".tmp_ffconcat_", suffix=".txt")
    os.close(fd_list)
    fd_out, tmp_out = tempfile.mkstemp(dir=parent, prefix=f".tmp_{os.path.basename(abs_target)}_", suffix=".mp3")
    os.close(fd_out)

    try:
        with open(concat_list_path, "w", encoding="utf-8") as cf:
            for p in mp3_paths:
                escaped = os.path.abspath(p).replace("'", "'\\''")
                cf.write(f"file '{escaped}'\n")

        if progress_cb:
            progress_cb(f"מפענח ומקודד מחדש {len(mp3_paths)} מקטעי MP3 באמצעות FFmpeg...")

        proc = subprocess.run(
            [
                ffmpeg_exe,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list_path,
                "-vn",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                tmp_out,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        _check_cancelled(cancel_event)
        if proc.returncode != 0 or not os.path.exists(tmp_out) or os.path.getsize(tmp_out) <= 0:
            err_tail = (proc.stderr or proc.stdout or "")[-400:]
            raise RuntimeError(f"FFmpeg נכשל באיחוד ה-MP3 (קוד {proc.returncode}): {err_tail}")

        with open(tmp_out, "rb+") as f_sync:
            f_sync.flush()
            os.fsync(f_sync.fileno())
        os.replace(tmp_out, abs_target)
        return abs_target
    finally:
        for tmp in (concat_list_path, tmp_out):
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass


def export_to_mp3(
    sentences: List[str],
    output_path: str,
    default_voice: str = "edge_avri",
    speed: float = 1.0,
    voice_overrides: Optional[List[str]] = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    cancel_event: Any = None,
    mp3_synth_fn: Optional[Callable[[str, str, float], bytes]] = None,
    mp3_merge_fn: Optional[Callable[[List[str], str], str]] = None,
) -> str:
    """
    מייצא קובץ MP3 איכותי באמצעות Edge-TTS (או מסנתז מוזרק לבדיקות):
    כל מקטע נשמר זמנית בדיסק ומאוחד בפענוח/קידוד תקני כשיש יותר ממקטע אחד.
    """
    _check_cancelled(cancel_event)

    segments = _group_segments(sentences, default_voice, voice_overrides)
    if not segments:
        raise ValueError("אין טקסט לייצוא.")

    rate_str = f"{int((speed - 1.0) * 100):+d}%"
    total = len(segments)
    temp_dir = tempfile.mkdtemp(prefix="pormycodex_mp3_parts_")
    part_paths: List[str] = []

    try:
        if mp3_synth_fn is not None:
            for i, (v_id, text_chunk) in enumerate(segments, 1):
                _check_cancelled(cancel_event)
                if progress_cb:
                    progress_cb(f"מייצר אודיו MP3: חלק {i} מתוך {total}...")
                voice_name = _voice_id_to_edge_name(v_id)
                part_bytes = mp3_synth_fn(text_chunk, voice_name, speed)
                if not part_bytes:
                    raise RuntimeError(f"התקבל מקטע MP3 ריק בחלק {i} מתוך {total}.")
                part_file = os.path.join(temp_dir, f"seg_{i:04d}.mp3")
                atomic_write_bytes(part_file, part_bytes)
                part_paths.append(part_file)
        else:
            import edge_tts

            async def synthesize_all() -> None:
                for i, (v_id, text_chunk) in enumerate(segments, 1):
                    _check_cancelled(cancel_event)
                    if progress_cb:
                        progress_cb(f"מייצר אודיו MP3: חלק {i} מתוך {total}...")
                    voice_name = _voice_id_to_edge_name(v_id)
                    part_file = os.path.join(temp_dir, f"seg_{i:04d}.mp3")
                    last_err: Optional[Exception] = None
                    for attempt in range(3):
                        _check_cancelled(cancel_event)
                        try:
                            communicate = edge_tts.Communicate(text_chunk, voice_name, rate=rate_str)
                            await communicate.save(part_file)
                            _check_cancelled(cancel_event)
                            if os.path.exists(part_file) and os.path.getsize(part_file) > 0:
                                last_err = None
                                break
                            last_err = RuntimeError(f"התקבל מקטע MP3 ריק בחלק {i} מתוך {total}.")
                        except ExportCancelledError:
                            raise
                        except Exception as exc:
                            last_err = exc
                            if attempt < 2:
                                await asyncio.sleep(0.5 * (attempt + 1))
                    if last_err is not None:
                        raise RuntimeError(f"כשל Edge-TTS בחלק {i}/{total}: {last_err}") from last_err
                    part_paths.append(part_file)

            asyncio.run(synthesize_all())

        _check_cancelled(cancel_event)
        if len(part_paths) != total:
            raise RuntimeError("ייצוא ה-MP3 נכשל: חסרים מקטעים.")

        if mp3_synth_fn is not None and mp3_merge_fn is None and len(part_paths) > 1 and find_ffmpeg_executable() is None:
            # במבחנים מוזרקים ללא ffmpeg, נבדוק שיש מפענח חוקי או נזרוק אם מצפים לאריזת WAV/ZIP
            return merge_mp3_files_decoded(
                part_paths,
                output_path,
                cancel_event=cancel_event,
                progress_cb=progress_cb,
                mp3_merge_fn=mp3_merge_fn,
            )

        return merge_mp3_files_decoded(
            part_paths,
            output_path,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            mp3_merge_fn=mp3_merge_fn,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


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
    """מייצא קובץ WAV אופליין מלא בהזרמה דרך הדיסק וכתיבה אטומית (זיכרון קבוע ומוגבל)."""
    _check_cancelled(cancel_event)

    segments = _group_segments(sentences, default_voice, voice_overrides, max_chars_per_batch=3000)
    if not segments:
        raise ValueError("אין טקסט לייצוא.")

    total = len(segments)
    temp_dir = tempfile.mkdtemp(prefix="pormycodex_wav_parts_")
    part_paths: List[str] = []

    try:
        if wav_synth_fn is not None:
            for i, (v_id, text_chunk) in enumerate(segments, 1):
                _check_cancelled(cancel_event)
                if progress_cb:
                    progress_cb(f"מייצר אודיו WAV: חלק {i} מתוך {total}...")
                wav_bytes = wav_synth_fn(text_chunk, v_id, speed)
                if not wav_bytes or len(wav_bytes) <= 44:
                    raise RuntimeError(f"התקבל מקטע WAV ריק/לא תקין בחלק {i} מתוך {total}.")
                with wave.open(io.BytesIO(wav_bytes), "rb") as w_chk:
                    if w_chk.getnframes() <= 0:
                        raise RuntimeError(f"לא התקבלו פריימים של שמע בחלק {i} מתוך {total}.")
                part_file = os.path.join(temp_dir, f"seg_{i:04d}.wav")
                atomic_write_bytes(part_file, wav_bytes)
                part_paths.append(part_file)
        else:
            import pyttsx3

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
                part_file = os.path.join(temp_dir, f"seg_{i:04d}.wav")
                part_paths.append(part_file)
                engine.save_to_file(text_chunk, part_file)
                if i % 5 == 0 or i == total:
                    engine.runAndWait()
                    _check_cancelled(cancel_event)

            for i, pw in enumerate(part_paths, 1):
                if not os.path.exists(pw) or os.path.getsize(pw) <= 44:
                    raise RuntimeError(f"מנוע SAPI5 נכשל ביצירת חלק {i} מתוך {total}.")
                with wave.open(pw, "rb") as w_chk:
                    if w_chk.getnframes() <= 0:
                        raise RuntimeError(
                            f"מנוע Windows SAPI5 יצר שמע ריק בחלק {i}/{total} — "
                            "כנראה שאין קול עברי מותקן ב-Windows SAPI5. בחר בקולות Edge-TTS (אברי/הילה)."
                        )

        if len(part_paths) != total:
            raise RuntimeError("ייצוא ה-WAV נכשל: חסרים מקטעי שמע.")

        return merge_wav_files_streaming(
            part_paths,
            output_path,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _convert_mp3_to_wav_ffmpeg(
    mp3_path: str,
    wav_output_path: str,
    *,
    cancel_event: Any = None,
) -> str:
    """ממיר קובץ MP3 לקובץ WAV תקני (PCM 16-bit 24kHz mono) באמצעות FFmpeg באופן אטומי."""
    _check_cancelled(cancel_event)
    ffmpeg_exe = find_ffmpeg_executable()
    if not ffmpeg_exe:
        raise RuntimeError("נדרש FFmpeg להמרת שמע Edge-TTS לפורמט WAV.")

    abs_target = os.path.abspath(wav_output_path)
    parent = os.path.dirname(abs_target)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_wav = tempfile.mkstemp(dir=parent, prefix=f".tmp_{os.path.basename(abs_target)}_", suffix=".wav")
    os.close(fd)
    try:
        proc = subprocess.run(
            [
                ffmpeg_exe,
                "-y",
                "-i",
                mp3_path,
                "-vn",
                "-acodec",
                "pcm_s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                tmp_wav,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        _check_cancelled(cancel_event)
        if proc.returncode != 0 or not os.path.exists(tmp_wav) or os.path.getsize(tmp_wav) <= 44:
            err_tail = (proc.stderr or proc.stdout or "")[-400:]
            raise RuntimeError(f"FFmpeg נכשל בהמרת MP3 ל-WAV (קוד {proc.returncode}): {err_tail}")
        with open(tmp_wav, "rb+") as f_sync:
            f_sync.flush()
            os.fsync(f_sync.fileno())
        os.replace(tmp_wav, abs_target)
        return abs_target
    finally:
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass


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
    mp3_merge_fn: Optional[Callable[[List[str], str], str]] = None,
) -> str:
    """
    מייצא קובץ שמע יחיד (.mp3 או .wav) באופן אטומי לפרק בודד.
    עבור WAV עם קולות Edge-TTS (כאשר FFmpeg זמין ואין wav_synth_fn מוזרק),
    מסנתז בקולות הנוירליים בעברית וממיר ל-WAV תקני — עובד גם במחשבי Windows ללא קול SAPI5 עברי!
    """
    _check_cancelled(cancel_event)

    ext = os.path.splitext(output_path)[1].lower()
    want_wav = ext == ".wav" or (default_voice == "sapi5" and ext != ".mp3")
    if want_wav:
        if ext != ".wav":
            output_path = f"{output_path}.wav"
        if (
            wav_synth_fn is None
            and str(default_voice or "").startswith("edge")
            and find_ffmpeg_executable() is not None
        ):
            tmp_dir = tempfile.mkdtemp(prefix="pormycodex_edge_wav_")
            try:
                tmp_mp3 = os.path.join(tmp_dir, "edge_chapter.mp3")
                export_to_mp3(
                    sentences,
                    tmp_mp3,
                    default_voice=default_voice,
                    speed=speed,
                    voice_overrides=voice_overrides,
                    progress_cb=progress_cb,
                    cancel_event=cancel_event,
                    mp3_synth_fn=mp3_synth_fn,
                    mp3_merge_fn=mp3_merge_fn,
                )
                if progress_cb:
                    progress_cb("ממיר שמע Edge-TTS לפורמט WAV תקני...")
                return _convert_mp3_to_wav_ffmpeg(tmp_mp3, output_path, cancel_event=cancel_event)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

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
            mp3_merge_fn=mp3_merge_fn,
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


def package_book_audio(
    *,
    book_id: str,
    book_title: str,
    chapter_entries: List[Dict[str, Any]],
    output_path: str,
    package_mode: str = "zip",
    cancel_event: Any = None,
    progress_cb: Optional[Callable[[str], None]] = None,
    mp3_merge_fn: Optional[Callable[[List[str], str], str]] = None,
) -> Dict[str, Any]:
    """
    אורז תוצר שמע שלם לספר מלא לפי הסדר:
    - 'unified_wav': איחוד כל פרקי ה-WAV לקובץ WAV תקין אחד בהזרמה
    - 'unified_mp3': איחוד כל פרקי ה-MP3 בפענוח/קידוד מחדש (FFmpeg / mp3_merge_fn)
    - 'zip': ארכיון ZIP אטומי עם קבצים ממוספרים לכל פרק + manifest.json
    - 'folder': תיקייה מסודרת עם קבצים ממוספרים לכל פרק + manifest.json
    """
    _check_cancelled(cancel_event)
    if not chapter_entries:
        raise ValueError("אין פרקים לאריזת שמע.")

    ordered = sorted(chapter_entries, key=lambda c: int(c["chapter_index"]))
    audio_paths = [c["audio_path"] for c in ordered]
    for c in ordered:
        p = c["audio_path"]
        if not p or not os.path.exists(p) or os.path.getsize(p) <= 0:
            raise RuntimeError(f"חסר קובץ שמע עבור פרק {int(c['chapter_index']) + 1} ('{c.get('chapter_title', '')}').")

    manifest_items = []
    for pos, ch in enumerate(ordered, 1):
        ext = os.path.splitext(ch["audio_path"])[1].lower() or ".wav"
        safe_ch = sanitize_filename_part(ch.get("chapter_title", ""), f"chapter_{pos}")
        numbered_name = f"{pos:03d}_{safe_ch}{ext}"
        manifest_items.append({
            "order": pos,
            "chapter_index": int(ch["chapter_index"]),
            "chapter_title": ch.get("chapter_title", f"פרק {pos}"),
            "filename": numbered_name,
            "size_bytes": os.path.getsize(ch["audio_path"]),
            "line_count": int(ch.get("line_count", 0)),
        })

    manifest = {
        "scope": "book",
        "book_id": book_id,
        "book_title": book_title,
        "package_mode": package_mode,
        "chapter_count": len(ordered),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "chapters": manifest_items,
    }

    if package_mode == "unified_wav":
        target = output_path if output_path.lower().endswith(".wav") else f"{os.path.splitext(output_path)[0]}.wav"
        saved = merge_wav_files_streaming(
            audio_paths,
            target,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
        )
        return {
            "output_path": saved,
            "package_mode": "unified_wav",
            "chapter_count": len(ordered),
            "manifest": manifest,
        }

    if package_mode == "unified_mp3":
        target = output_path if output_path.lower().endswith(".mp3") else f"{os.path.splitext(output_path)[0]}.mp3"
        saved = merge_mp3_files_decoded(
            audio_paths,
            target,
            cancel_event=cancel_event,
            progress_cb=progress_cb,
            mp3_merge_fn=mp3_merge_fn,
        )
        return {
            "output_path": saved,
            "package_mode": "unified_mp3",
            "chapter_count": len(ordered),
            "manifest": manifest,
        }

    if package_mode == "folder":
        target_dir = os.path.abspath(output_path)
        os.makedirs(target_dir, exist_ok=True)
        for ch, m_item in zip(ordered, manifest_items):
            _check_cancelled(cancel_event)
            if progress_cb:
                progress_cb(f"מעתיק קובץ פרק {m_item['order']}/{len(ordered)} לתיקייה...")
            dest_file = os.path.join(target_dir, m_item["filename"])
            fd, tmp_f = tempfile.mkstemp(dir=target_dir, prefix=f".tmp_{m_item['filename']}_", suffix=".part")
            os.close(fd)
            try:
                with open(ch["audio_path"], "rb") as src_f, open(tmp_f, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f, length=65536)
                    dst_f.flush()
                    os.fsync(dst_f.fileno())
                os.replace(tmp_f, dest_file)
            except Exception:
                if os.path.exists(tmp_f):
                    try:
                        os.remove(tmp_f)
                    except Exception:
                        pass
                raise
        manifest_path = os.path.join(target_dir, "manifest.json")
        atomic_write_json(manifest_path, manifest)
        return {
            "output_path": target_dir,
            "manifest_path": manifest_path,
            "package_mode": "folder",
            "chapter_count": len(ordered),
            "manifest": manifest,
        }

    # Default: atomic ZIP archive of numbered chapter audio files + manifest.json
    target_zip = output_path if output_path.lower().endswith(".zip") else f"{os.path.splitext(output_path)[0]}.zip"
    abs_zip = os.path.abspath(target_zip)
    parent = os.path.dirname(abs_zip)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_zip = tempfile.mkstemp(dir=parent, prefix=f".tmp_{os.path.basename(abs_zip)}_", suffix=".zip.part")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            for ch, m_item in zip(ordered, manifest_items):
                _check_cancelled(cancel_event)
                if progress_cb:
                    progress_cb(f"אורז פרק {m_item['order']}/{len(ordered)} לארכיון ZIP...")
                zf.write(ch["audio_path"], arcname=m_item["filename"])
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        _check_cancelled(cancel_event)
        with open(tmp_zip, "rb+") as f_sync:
            f_sync.flush()
            os.fsync(f_sync.fileno())
        os.replace(tmp_zip, abs_zip)
        return {
            "output_path": abs_zip,
            "package_mode": "zip",
            "chapter_count": len(ordered),
            "manifest": manifest,
        }
    except Exception:
        if os.path.exists(tmp_zip):
            try:
                os.remove(tmp_zip)
            except Exception:
                pass
        raise
