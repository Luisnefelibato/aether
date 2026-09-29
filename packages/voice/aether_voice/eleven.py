from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

import httpx
import numpy as np

from aether_core.config import Settings
from aether_core.events import EventBus, SessionState
from aether_voice.speakable import speakable

logger = logging.getLogger("aether.voice")


class ElevenLabsTTS:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.settings = settings
        self.bus = bus
        self._client = httpx.AsyncClient(timeout=60.0)
        self._cache: dict[str, bytes] = {}
        self._playing = False
        self._cancel = asyncio.Event()
        bus.on("barge_in", self._on_barge)
        bus.on("cancel", self._on_barge)

    async def _on_barge(self, _event: Any = None) -> None:
        self._cancel.set()

    async def speak(self, text: str) -> None:
        text = speakable((text or "").strip())
        if not text:
            return
        self._cancel.clear()
        self.bus.set_state(SessionState.SPEAKING)
        await self.bus.publish("tts_start", text=text)

        if not self.settings.elevenlabs_api_key:
            logger.warning("ELEVENLABS_API_KEY missing — printing speech")
            print(f"[ADAM]: {text}")
            await asyncio.sleep(min(2.5, 0.03 * len(text)))
            await self.bus.publish("tts_end", text=text)
            return

        try:
            audio = await self._synthesize(text)
            if self._cancel.is_set():
                return
            await self._play_mp3(audio)
        except Exception as exc:  # noqa: BLE001
            logger.exception("TTS failed: %s", exc)
            print(f"[ADAM]: {text}")
        finally:
            await self.bus.publish("tts_end", text=text)

    async def _synthesize(self, text: str) -> bytes:
        if text in self._cache:
            return self._cache[text]
        voice_id = self.settings.elevenlabs_voice_id
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
        payload = {
            "text": text,
            "model_id": self.settings.voice.model_id,
            "voice_settings": {
                "stability": self.settings.voice.stability,
                "similarity_boost": self.settings.voice.similarity_boost,
                "style": self.settings.voice.style,
                "use_speaker_boost": True,
            },
        }
        resp = await self._client.post(
            url,
            headers={
                "xi-api-key": self.settings.elevenlabs_api_key,
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
            },
            json=payload,
            params={"output_format": "mp3_44100_128"},
        )
        resp.raise_for_status()
        data = resp.content
        if len(text) < 80:
            self._cache[text] = data
        return data

    async def _play_mp3(self, data: bytes) -> None:
        def _decode_and_play() -> None:
            try:
                import time

                import sounddevice as sd
                import soundfile as sf
            except ImportError:
                logger.warning("sounddevice/soundfile missing")
                return
            with io.BytesIO(data) as bio:
                audio, sr = sf.read(bio, dtype="float32")
            if self._cancel.is_set():
                return
            frames = len(audio) if getattr(audio, "ndim", 1) == 1 else len(audio)
            duration = float(frames) / float(sr)
            sd.play(audio, sr, blocking=False)
            elapsed = 0.0
            step = 0.05
            while elapsed < duration:
                if self._cancel.is_set():
                    sd.stop()
                    return
                time.sleep(step)
                elapsed += step
            sd.wait()

        await asyncio.to_thread(_decode_and_play)
