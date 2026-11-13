# -*- coding: utf-8 -*-
"""
מנוע אודיו והקראה קולית שולחני (Audio Engine) עבור PorMyCodex.
תומך ב-Edge-TTS (קולות נוירליים עבריים באיכות אולפן: Avri ו-Hila)
וב-pyttsx3 (SAPI5 מקומי ב-Windows Offline מלא).
כולל תמיכה בהקראת תסריט רב-קולי (מורה בקול אברי, תלמיד בקול הילה).
"""

import os
import tempfile
import asyncio
from typing import List, Optional
from PyQt6.QtCore import QObject, QThread, pyqtSignal

PYGAME_AVAILABLE = False
try:
    import pygame
    pygame.mixer.init()
    PYGAME_AVAILABLE = True
except Exception:
    pass


class PlaybackWorker(QThread):
    sentence_started = pyqtSignal(int)
    playback_finished = pyqtSignal()
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

    def run(self):
        while self._is_running and self.current_index < len(self.sentences):
            if self._is_paused:
                self.msleep(100)
                continue

            text = self.sentences[self.current_index].strip()
            if not text:
                self.current_index += 1
                continue

            self.sentence_started.emit(self.current_index)

            active_voice = self.voice_type
            if 0 <= self.current_index < len(self.voice_overrides) and self.voice_overrides[self.current_index]:
                active_voice = self.voice_overrides[self.current_index]

            success = self._play_sentence(text, active_voice)
            if not success and not self._is_running:
                break

            self.current_index += 1

        self.playback_finished.emit()

    def _play_sentence(self, text: str, voice_type: str) -> bool:
        if "edge" in voice_type:
            return self._play_with_edge_tts(text, voice_type)
        else:
            return self._play_with_pyttsx3(text)

    def _play_with_edge_tts(self, text: str, voice_type: str) -> bool:
        """משתמש ב-Edge-TTS להשמעת קול נוירלי עברי באיכות גבוהה."""
        voice_name = "he-IL-AvriNeural" if "avri" in voice_type else "he-IL-HilaNeural"
        rate_str = f"{int((self.speed - 1.0) * 100):+d}%"

        temp_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        temp_path = temp_file.name
        temp_file.close()

        try:
            import edge_tts

            async def generate():
                communicate = edge_tts.Communicate(text, voice_name, rate=rate_str)
                await communicate.save(temp_path)

            asyncio.run(generate())

            if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
                raise RuntimeError("קובץ אודיו ריק")

            if PYGAME_AVAILABLE:
                pygame.mixer.music.load(temp_path)
                pygame.mixer.music.play()
                while (pygame.mixer.music.get_busy() or self._is_paused) and self._is_running:
                    self.msleep(50)
                pygame.mixer.music.stop()
                try:
                    pygame.mixer.music.unload()
                except Exception:
                    pass
            else:
                self._play_with_pyttsx3(text)

            return True
        except Exception:
            return self._play_with_pyttsx3(text)
        finally:
            try:
                if PYGAME_AVAILABLE:
                    pygame.mixer.music.unload()
            except Exception:
                pass
            try:
                if os.path.exists(temp_path):
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
