# -*- coding: utf-8 -*-
"""
מנוע אודיו והקראה קולית שולחני (Audio Engine) עבור PorMyCodex.
תומך ב-Edge-TTS (קולות נוירליים עבריים באיכות אולפן: Avri ו-Hila)
וב-pyttsx3 (SAPI5 מקומי ב-Windows Offline מלא).
כולל תמיכה בהקראת תסריט רב-קולי (מורה בקול אברי, תלמיד בקול הילה).
"""

import asyncio
import concurrent.futures
import os
import tempfile
import time
from typing import Dict, List, Optional
from PyQt6.QtCore import QObject, QThread, pyqtSignal

PYGAME_AVAILABLE = False
try:
    import pygame
    pygame.mixer.init()
    PYGAME_AVAILABLE = True
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
    ):
        super().__init__()
        self.sentences = sentences
        self.current_index = start_index
        self.voice_type = voice_type
        self.speed = speed
        self.voice_overrides = voice_overrides or []
        self._is_running = True
        self._is_paused = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._prefetch_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self._prefetched: Dict[int, str] = {}

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
            if not self._is_running:
                break
            try:
                async def generate():
                    communicate = edge_tts.Communicate(text, voice_name, rate=rate_str)
                    await communicate.save(temp_path)

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(generate())
                finally:
                    loop.close()

                if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                    return temp_path
                last_err = RuntimeError("קובץ אודיו ריק")
            except Exception as exc:
                last_err = exc
                if attempt == 0 and self._is_running:
                    time.sleep(0.25)

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

    def run(self):
        com_inited = _co_init_windows()
        completed_naturally = False
        next_future: Optional[concurrent.futures.Future] = None
        next_future_idx = -1

        try:
            while self._is_running and self.current_index < len(self.sentences):
                if self._is_paused:
                    self.msleep(80)
                    continue

                text = self.sentences[self.current_index].strip()
                if not text:
                    self.current_index += 1
                    continue

                self.sentence_started.emit(self.current_index)
                active_voice = self._voice_for_index(self.current_index)

                prefetched_path: Optional[str] = None
                if next_future is not None and next_future_idx == self.current_index:
                    try:
                        prefetched_path = next_future.result(timeout=12.0)
                    except Exception:
                        prefetched_path = None
                    next_future = None
                    next_future_idx = -1
                elif next_future is not None:
                    next_future.cancel()
                    next_future = None
                    next_future_idx = -1

                # טעינה מקדימה של המשפט הבא ברקע בזמן ההשמעה הנוכחית
                lookahead = self.current_index + 1
                while lookahead < len(self.sentences) and not self.sentences[lookahead].strip():
                    lookahead += 1
                if lookahead < len(self.sentences):
                    nxt_voice = self._voice_for_index(lookahead)
                    if "edge" in nxt_voice and PYGAME_AVAILABLE:
                        nxt_text = self.sentences[lookahead].strip()
                        next_future_idx = lookahead
                        next_future = self._prefetch_executor.submit(
                            self._synthesize_edge_file, nxt_text, nxt_voice
                        )

                success = self._play_sentence(text, active_voice, prefetched_path=prefetched_path)
                if not success and not self._is_running:
                    break

                self.current_index += 1

            if self._is_running and self.current_index >= len(self.sentences):
                completed_naturally = True
        finally:
            if next_future is not None:
                try:
                    p = next_future.result(timeout=0.2) if next_future.done() else None
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            self._cleanup_prefetched()
            self._prefetch_executor.shutdown(wait=False)
            _co_uninit_windows(com_inited)

        if completed_naturally:
            self.chapter_finished.emit()
        self.playback_finished.emit()

    def _play_sentence(self, text: str, voice_type: str, prefetched_path: Optional[str] = None) -> bool:
        if "edge" in voice_type:
            return self._play_with_edge_tts(text, voice_type, prefetched_path=prefetched_path)
        return self._play_with_pyttsx3(text)

    def _play_with_edge_tts(self, text: str, voice_type: str, prefetched_path: Optional[str] = None) -> bool:
        """משתמש ב-Edge-TTS להשמעת קול נוירלי עברי באיכות גבוהה עם טעינה מקדימה וגיבוי מקומי."""
        temp_path = prefetched_path or ""
        try:
            if not temp_path or not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                temp_path = self._synthesize_edge_file(text, voice_type)

            if PYGAME_AVAILABLE:
                pygame.mixer.music.load(temp_path)
                pygame.mixer.music.play()
                while (pygame.mixer.music.get_busy() or self._is_paused) and self._is_running:
                    self.msleep(40)
                pygame.mixer.music.stop()
                try:
                    pygame.mixer.music.unload()
                except Exception:
                    pass
            else:
                return self._play_with_pyttsx3(text)

            return True
        except Exception as exc:
            if not self._is_running:
                return False
            self.playback_error.emit(f"Edge-TTS לא זמין זמנית ({exc}), עובר להקראה מקומית.")
            return self._play_with_pyttsx3(text)
        finally:
            try:
                if PYGAME_AVAILABLE:
                    pygame.mixer.music.unload()
            except Exception:
                pass
            try:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
            except Exception:
                pass

    def _play_with_pyttsx3(self, text: str) -> bool:
        """הקראה במנוע SAPI5 המקומי של Windows (ללא צורך באינטרנט)."""
        try:
            import pyttsx3
            engine = pyttsx3.init()
            base_rate = engine.getProperty('rate')
            engine.setProperty('rate', int(base_rate * self.speed))

            voices = engine.getProperty('voices')
            for v in voices:
                if 'hebrew' in v.name.lower() or 'he-il' in str(v.languages).lower() or 'asaf' in v.name.lower() or 'hila' in v.name.lower():
                    engine.setProperty('voice', v.id)
                    break

            engine.say(text)
            engine.runAndWait()
            return True
        except Exception as e:
            self.playback_error.emit(f"שגיאת SAPI5: {e}")
            return False

    def stop(self):
        self._is_running = False
        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass

    def pause(self):
        self._is_paused = True
        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.music.pause()
            except Exception:
                pass

    def resume(self):
        self._is_paused = False
        if PYGAME_AVAILABLE:
            try:
                pygame.mixer.music.unpause()
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

    def play(self, sentences: List[str], start_index: int = 0, voice_overrides: Optional[List[str]] = None):
        self.stop()
        if not sentences:
            return

        self.worker = PlaybackWorker(
            sentences=sentences,
            start_index=start_index,
            voice_type=self.voice_type,
            speed=self.speed,
            voice_overrides=voice_overrides,
        )
        self.worker.sentence_started.connect(self.sentence_changed.emit)
        self.worker.chapter_finished.connect(self.chapter_completed.emit)
        self.worker.playback_finished.connect(lambda: self.state_changed.emit(False))
        self.worker.playback_error.connect(self.error_occurred.emit)
        self.worker.start()
        self.state_changed.emit(True)

    def stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(1000)
            self.worker = None
        self.state_changed.emit(False)

    def pause(self):
        if self.worker:
            self.worker.pause()
            self.state_changed.emit(False)

    def resume(self):
        if self.worker:
            self.worker.resume()
            self.state_changed.emit(True)

    def is_playing(self) -> bool:
        return self.worker is not None and self.worker.isRunning() and not self.worker._is_paused
