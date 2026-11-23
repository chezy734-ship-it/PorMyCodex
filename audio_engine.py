# -*- coding: utf-8 -*-
"""
מנוע אודיו והקראה קולית שולחני (Audio Engine) עבור PorMyCodex.
תומך ב-Edge-TTS (קולות נוירליים עבריים באיכות אולפן: Avri ו-Hila)
וב-pyttsx3 (SAPI5 מקומי ב-Windows Offline מלא).
כולל תמיכה בהקראת תסריט רב-קולי (מורה בקול אברי, תלמיד בקול הילה),
נעילת גישה ל-pygame.mixer, מניעת השמדת QThread פעיל (קריסות C++),
ו-generation token המונע מ-worker ישן לדרוס השמעה חדשה.
"""

import asyncio
import concurrent.futures
import os
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from PyQt6.QtCore import QObject, QThread, pyqtSignal

_MIXER_LOCK = threading.RLock()
_PYTTSX3_LOCK = threading.Lock()
_RETIRED_LOCK = threading.Lock()
_RETIRED_WORKERS: List["PlaybackWorker"] = []

PYGAME_AVAILABLE = False
_pygame_mod: Any = None
try:
    import pygame as _pg
    _pygame_mod = _pg
    try:
        if not _pg.mixer.get_init():
            _pg.mixer.init()
        PYGAME_AVAILABLE = bool(_pg.mixer.get_init())
    except Exception:
        PYGAME_AVAILABLE = False
except Exception:
    _pygame_mod = None
    PYGAME_AVAILABLE = False


def _ensure_pygame_mixer() -> bool:
    global PYGAME_AVAILABLE
    if _pygame_mod is None:
        PYGAME_AVAILABLE = False
        return False
    with _MIXER_LOCK:
        try:
            if not _pygame_mod.mixer.get_init():
                _pygame_mod.mixer.init()
            PYGAME_AVAILABLE = bool(_pygame_mod.mixer.get_init())
            return PYGAME_AVAILABLE
        except Exception:
            PYGAME_AVAILABLE = False
            return False


def _retire_playback_worker(worker: Optional["PlaybackWorker"], wait_ms: int = 250) -> None:
    """
    עוצר PlaybackWorker בצורה בטוחה בלי לעולם להשמיד QThread שעדיין רץ
    (שגורם ל-'QThread: Destroyed while thread is still running' ולקריסת abort מיידית).
    """
    if worker is None:
        return
    try:
        worker.stop()
    except Exception:
        pass
    try:
        if worker.isRunning():
            worker.wait(max(0, int(wait_ms)))
    except Exception:
        pass
    try:
        still_running = worker.isRunning()
    except Exception:
        still_running = False
    if still_running:
        with _RETIRED_LOCK:
            if worker not in _RETIRED_WORKERS:
                _RETIRED_WORKERS.append(worker)

        def _drop(w=worker):
            with _RETIRED_LOCK:
                if w in _RETIRED_WORKERS:
                    _RETIRED_WORKERS.remove(w)

        try:
            worker.finished.connect(_drop)
        except Exception:
            pass


def _co_init_windows() -> bool:
    if os.name != "nt":
        return False
    try:
        import pythoncom  # type: ignore
        pythoncom.CoInitialize()
        return True
    except Exception:
        return False


def _co_uninit_windows(did_init: bool) -> None:
    if not did_init:
        return
    try:
        import pythoncom  # type: ignore
        pythoncom.CoUninitialize()
    except Exception:
        pass


class PlaybackWorker(QThread):
    sentence_started = pyqtSignal(int)
    playback_finished = pyqtSignal()
    chapter_finished = pyqtSignal()
    playback_error = pyqtSignal(str)

    def __init__(
        self,
        sentences: List[str],
        start_index: int = 0,
        voice_type: str = "edge_avri",
        speed: float = 1.0,
        voice_overrides: Optional[List[str]] = None,
        is_active_fn: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(None)
        self.sentences = list(sentences or [])
        self.current_index = max(0, int(start_index))
        self.voice_type = voice_type or "edge_avri"
        self.speed = max(0.5, min(2.5, float(speed or 1.0)))
        self.voice_overrides = list(voice_overrides or [])
        self._is_active_fn = is_active_fn
        self._is_running = True
        self._is_paused = False
        self._stop_event = threading.Event()
        self._active_pyttsx3_engine: Any = None
        self._engine_lock = threading.Lock()
        self._prefetched: Dict[int, str] = {}

    def _still_active(self) -> bool:
        if not self._is_running or self._stop_event.is_set():
            return False
        if self._is_active_fn is not None:
            try:
                return bool(self._is_active_fn())
            except Exception:
                return False
        return True

    def _voice_for_index(self, idx: int) -> str:
        if 0 <= idx < len(self.voice_overrides) and self.voice_overrides[idx]:
            return self.voice_overrides[idx]
        return self.voice_type

    def _synthesize_edge_file(self, text: str, voice_type: str) -> str:
        import edge_tts

        voice_name = "he-IL-AvriNeural" if "avri" in (voice_type or "").lower() else "he-IL-HilaNeural"
        rate_str = f"{int((self.speed - 1.0) * 100):+d}%"
        temp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        temp_path = temp_file.name
        temp_file.close()

        last_err: Optional[Exception] = None
        for attempt in range(2):
            if not self._still_active():
                break
            try:
                async def generate():
                    communicate = edge_tts.Communicate(text, voice_name, rate=rate_str)
                    await communicate.save(temp_path)

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(generate())
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass

                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    return temp_path
                last_err = RuntimeError("קובץ אודיו ריק")
            except Exception as exc:
                last_err = exc
                if attempt == 0 and self._still_active():
                    self._stop_event.wait(0.25)

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        raise RuntimeError(str(last_err or "שגיאה בסינתזת Edge-TTS"))

    def _cleanup_prefetched(self) -> None:
        for p in list(self._prefetched.values()):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        self._prefetched.clear()

    def _wait_future_interruptible(
        self,
        fut: concurrent.futures.Future,
        timeout_sec: float = 12.0,
    ) -> Optional[str]:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not self._still_active():
                fut.cancel()
                return None
            if fut.done():
                try:
                    return fut.result(timeout=0.05)
                except Exception:
                    return None
            self.msleep(45)
        fut.cancel()
        return None

    def run(self):
        com_inited = _co_init_windows()
        completed_naturally = False
        next_future: Optional[concurrent.futures.Future] = None
        next_future_idx = -1
        prefetch_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        try:
            while self._still_active() and self.current_index < len(self.sentences):
                if self._is_paused:
                    self.msleep(60)
                    continue

                text = (self.sentences[self.current_index] or "").strip()
                if not text:
                    self.current_index += 1
                    continue

                if not self._still_active():
                    break

                self.sentence_started.emit(self.current_index)
                active_voice = self._voice_for_index(self.current_index)

                prefetched_path: Optional[str] = None
                if next_future is not None and next_future_idx == self.current_index:
                    prefetched_path = self._wait_future_interruptible(next_future, timeout_sec=12.0)
                    next_future = None
                    next_future_idx = -1
                elif next_future is not None:
                    next_future.cancel()
                    next_future = None
                    next_future_idx = -1

                if not self._still_active():
                    if prefetched_path and os.path.exists(prefetched_path):
                        try:
                            os.remove(prefetched_path)
                        except Exception:
                            pass
                    break

                # טעינה מקדימה של המשפט הבא ברקע בזמן ההשמעה הנוכחית
                lookahead = self.current_index + 1
                while lookahead < len(self.sentences) and not (self.sentences[lookahead] or "").strip():
                    lookahead += 1
                if lookahead < len(self.sentences) and self._still_active():
                    nxt_voice = self._voice_for_index(lookahead)
                    if "edge" in nxt_voice and PYGAME_AVAILABLE:
                        nxt_text = self.sentences[lookahead].strip()
                        next_future_idx = lookahead
                        try:
                            next_future = prefetch_executor.submit(
                                self._synthesize_edge_file, nxt_text, nxt_voice
                            )
                        except Exception:
                            next_future = None
                            next_future_idx = -1

                success = self._play_sentence(text, active_voice, prefetched_path=prefetched_path)
                if not success and not self._still_active():
                    break

                self.current_index += 1

            if self._still_active() and self.current_index >= len(self.sentences):
                completed_naturally = True
        except Exception as exc:
            if self._still_active():
                try:
                    self.playback_error.emit(f"שגיאת מנוע השמעה: {exc}")
                except Exception:
                    pass
        finally:
            if next_future is not None:
                try:
                    next_future.cancel()
                    p = next_future.result(timeout=0.05) if next_future.done() and not next_future.cancelled() else None
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            self._cleanup_prefetched()
            try:
                prefetch_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                try:
                    prefetch_executor.shutdown(wait=False)
                except Exception:
                    pass
            except Exception:
                pass
            _co_uninit_windows(com_inited)

        if completed_naturally and self._still_active():
            self.chapter_finished.emit()
        self.playback_finished.emit()

    def _play_sentence(self, text: str, voice_type: str, prefetched_path: Optional[str] = None) -> bool:
        if not self._still_active():
            return False
        if "edge" in (voice_type or "").lower():
            return self._play_with_edge_tts(text, voice_type, prefetched_path=prefetched_path)
        return self._play_with_pyttsx3(text)

    def _play_with_edge_tts(self, text: str, voice_type: str, prefetched_path: Optional[str] = None) -> bool:
        """משתמש ב-Edge-TTS להשמעת קול נוירלי עברי באיכות גבוהה עם טעינה מקדימה וגיבוי מקומי."""
        temp_path = prefetched_path or ""
        loaded_into_mixer = False
        try:
            if not self._still_active():
                return False

            if not temp_path or not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                temp_path = self._synthesize_edge_file(text, voice_type)

            if not self._still_active():
                return False

            if _ensure_pygame_mixer() and _pygame_mod is not None:
                with _MIXER_LOCK:
                    if not self._still_active():
                        return False
                    _pygame_mod.mixer.music.load(temp_path)
                    _pygame_mod.mixer.music.play()
                    loaded_into_mixer = True

                while self._still_active():
                    with _MIXER_LOCK:
                        try:
                            busy = bool(_pygame_mod.mixer.music.get_busy())
                        except Exception:
                            busy = False
                    if not busy and not self._is_paused:
                        break
                    self.msleep(40)

                if loaded_into_mixer and self._still_active():
                    with _MIXER_LOCK:
                        try:
                            _pygame_mod.mixer.music.stop()
                            _pygame_mod.mixer.music.unload()
                        except Exception:
                            pass
                    loaded_into_mixer = False
            else:
                return self._play_with_pyttsx3(text)

            return True
        except Exception as exc:
            if not self._still_active():
                return False
            self.playback_error.emit(f"Edge-TTS לא זמין זמנית ({exc}), עובר להקראה מקומית.")
            return self._play_with_pyttsx3(text)
        finally:
            if loaded_into_mixer and _pygame_mod is not None and self._still_active():
                with _MIXER_LOCK:
                    try:
                        _pygame_mod.mixer.music.unload()
                    except Exception:
                        pass
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

    def _play_with_pyttsx3(self, text: str) -> bool:
        """הקראה במנוע SAPI5 המקומי של Windows (ללא צורך באינטרנט) עם הגנת נעילה מוחלטת."""
        if not self._still_active():
            return False
        with _PYTTSX3_LOCK:
            if not self._still_active():
                return False
            engine = None
            try:
                import pyttsx3
                engine = pyttsx3.init()
                with self._engine_lock:
                    self._active_pyttsx3_engine = engine
                base_rate = engine.getProperty("rate")
                engine.setProperty("rate", max(80, int(base_rate * self.speed)))

                voices = engine.getProperty("voices") or []
                for v in voices:
                    v_name = (getattr(v, "name", "") or "").lower()
                    v_langs = str(getattr(v, "languages", "") or "").lower()
                    if "hebrew" in v_name or "he-il" in v_langs or "asaf" in v_name or "hila" in v_name:
                        engine.setProperty("voice", v.id)
                        break

                if not self._still_active():
                    return False
                engine.say(text)
                engine.runAndWait()
                return True
            except Exception as e:
                if self._still_active():
                    self.playback_error.emit(f"שגיאת SAPI5: {e}")
                return False
            finally:
                with self._engine_lock:
                    self._active_pyttsx3_engine = None
                if engine is not None:
                    try:
                        engine.stop()
                    except Exception:
                        pass

    def stop(self):
        self._is_running = False
        self._is_paused = False
        self._stop_event.set()
        if PYGAME_AVAILABLE and _pygame_mod is not None:
            with _MIXER_LOCK:
                try:
                    _pygame_mod.mixer.music.stop()
                    _pygame_mod.mixer.music.unload()
                except Exception:
                    pass
        with self._engine_lock:
            eng = self._active_pyttsx3_engine
        if eng is not None:
            try:
                eng.stop()
            except Exception:
                pass

    def pause(self):
        self._is_paused = True
        if PYGAME_AVAILABLE and _pygame_mod is not None:
            with _MIXER_LOCK:
                try:
                    _pygame_mod.mixer.music.pause()
                except Exception:
                    pass

    def resume(self):
        self._is_paused = False
        if PYGAME_AVAILABLE and _pygame_mod is not None:
            with _MIXER_LOCK:
                try:
                    _pygame_mod.mixer.music.unpause()
                except Exception:
                    pass


class AudioEngine(QObject):
    sentence_changed = pyqtSignal(int)
    chapter_completed = pyqtSignal()
    state_changed = pyqtSignal(bool)
    error_occurred = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.worker: Optional[PlaybackWorker] = None
        self.voice_type = "edge_avri"
        self.speed = 1.0
        self._play_gen = 0

    def _retire_current_worker(self, wait_ms: int = 250) -> None:
        w = self.worker
        self.worker = None
        if w is None:
            return
        for sig in (w.sentence_started, w.chapter_finished, w.playback_finished, w.playback_error):
            try:
                sig.disconnect()
            except Exception:
                pass
        _retire_playback_worker(w, wait_ms=wait_ms)

    def play(self, sentences: List[str], start_index: int = 0, voice_overrides: Optional[List[str]] = None):
        self.stop()
        if not sentences:
            return

        self._play_gen += 1
        my_gen = self._play_gen
        safe_start = max(0, min(len(sentences) - 1, int(start_index)))

        w = PlaybackWorker(
            sentences=sentences,
            start_index=safe_start,
            voice_type=self.voice_type or "edge_avri",
            speed=self.speed,
            voice_overrides=voice_overrides,
            is_active_fn=lambda g=my_gen: self._play_gen == g,
        )
        self.worker = w

        w.sentence_started.connect(lambda idx, g=my_gen: self._on_worker_sentence(g, idx))
        w.chapter_finished.connect(lambda g=my_gen: self._on_worker_chapter_done(g))
        w.playback_finished.connect(lambda g=my_gen: self._on_worker_finished(g))
        w.playback_error.connect(lambda msg, g=my_gen: self._on_worker_error(g, msg))
        w.start()
        self.state_changed.emit(True)

    def _on_worker_sentence(self, gen: int, idx: int):
        if gen == self._play_gen:
            self.sentence_changed.emit(idx)

    def _on_worker_chapter_done(self, gen: int):
        if gen == self._play_gen:
            self.chapter_completed.emit()

    def _on_worker_finished(self, gen: int):
        if gen == self._play_gen:
            self.state_changed.emit(False)

    def _on_worker_error(self, gen: int, msg: str):
        if gen == self._play_gen:
            self.error_occurred.emit(msg)

    def stop(self):
        self._play_gen += 1
        self._retire_current_worker(wait_ms=250)
        self.state_changed.emit(False)

    def pause(self):
        if self.worker and self.worker.isRunning():
            self.worker.pause()
            self.state_changed.emit(False)

    def resume(self):
        if self.worker and self.worker.isRunning():
            self.worker.resume()
            self.state_changed.emit(True)

    def is_playing(self) -> bool:
        try:
            return self.worker is not None and self.worker.isRunning() and not self.worker._is_paused
        except RuntimeError:
            self.worker = None
            return False

    def shutdown(self, wait_ms: int = 1200) -> None:
        self._play_gen += 1
        self._retire_current_worker(wait_ms=wait_ms)
        with _RETIRED_LOCK:
            stragglers = list(_RETIRED_WORKERS)
        for w in stragglers:
            try:
                w.stop()
                if w.isRunning():
                    w.wait(min(400, max(100, wait_ms // 3)))
            except Exception:
                pass
