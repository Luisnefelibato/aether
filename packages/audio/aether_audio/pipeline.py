from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from collections import deque
from typing import Any

import httpx
import numpy as np

from aether_core.config import Settings
from aether_core.events import EventBus, SessionState

logger = logging.getLogger("aether.audio")


class AudioPipeline:
    def __init__(self, settings: Settings, bus: EventBus) -> None:
        self.settings = settings
        self.bus = bus
        self._running = False
        self._ptt_active = False
        self._listen_armed = False
        self._frames: deque[np.ndarray] = deque(maxlen=500)
        self._stream = None
        self._stt: SpeechToText | None = None
        self._wake: WakeWordEngine | None = None
        self._listener = None
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._utterance_buffer: list[np.ndarray] = []
        self._speech_started = False
        self._last_voice_ts = 0.0
        self._armed_at = 0.0
        self._speaking_ref = False
        self._finalizing = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._running = True
        self.bus.on("ptt", self._on_ptt)
        self.bus.on("state", self._on_state)

        self._stt = SpeechToText(self.settings)
        await self._stt.load()

        self._wake = WakeWordEngine(self.settings)
        try:
            await self._wake.load()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Wake engine disabled: %s", exc)

        try:
            self._start_hotkey()
        except Exception as exc:  # noqa: BLE001
            logger.warning("PTT hotkey disabled: %s", exc)
        try:
            self._start_mic()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mic disabled: %s", exc)

        self._tasks.append(asyncio.create_task(self._process_loop()))
        logger.info(
            "Audio pipeline started (stt=%s provider=%s mic=%s)",
            self._stt.ready,
            self._stt.provider,
            self._stream is not None,
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self._listener is not None:
            self._listener.stop()

    async def _on_ptt(self, event: Any) -> None:
        active = bool(event.payload.get("active"))
        if active:
            # Ignore duplicate downs while already listening (HUD+hotkey races).
            if self._ptt_active or self._listen_armed or self._finalizing:
                return
            self._ptt_active = True
            self._arm_listen("ptt")
        else:
            if self._finalizing:
                return
            self._ptt_active = False
            await self._finalize_utterance(reason="ptt_up")

    async def _on_state(self, event: Any) -> None:
        state = event.payload.get("state")
        self._speaking_ref = state == SessionState.SPEAKING.value

    def _arm_listen(self, source: str) -> None:
        self._listen_armed = True
        self._utterance_buffer.clear()
        self._speech_started = False
        now = time.time()
        self._last_voice_ts = now
        self._armed_at = now
        self.bus.set_state(SessionState.LISTENING)
        asyncio.create_task(self.bus.publish("listen_armed", source=source))
        logger.info("Listening armed via %s", source)

    def _start_hotkey(self) -> None:
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning("pynput missing — PTT hotkey disabled")
            return

        combo = self.settings.audio.ptt_hotkey.lower().replace(" ", "")
        parts = set(combo.split("+"))
        need_ctrl = "ctrl" in parts
        need_alt = "alt" in parts
        need_shift = "shift" in parts
        key_name = [p for p in parts if p not in {"ctrl", "alt", "shift"}]
        target = key_name[0] if key_name else "space"
        pressed: set[str] = set()

        def matches() -> bool:
            has_ctrl = any(k in pressed for k in ("ctrl", "ctrl_l", "ctrl_r"))
            has_alt = any(k in pressed for k in ("alt", "alt_l", "alt_r"))
            has_shift = any(k in pressed for k in ("shift", "shift_l", "shift_r"))
            has_key = target in pressed
            if need_ctrl and not has_ctrl:
                return False
            if need_alt and not has_alt:
                return False
            if need_shift and not has_shift:
                return False
            return has_key

        def on_press(key):
            pressed.add(_key_name(key))
            if matches() and not self._ptt_active and self._loop:
                self._ptt_active = True
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self.bus.publish("ptt", active=True))
                )

        def on_release(key):
            pressed.discard(_key_name(key))
            if self._ptt_active and not matches() and self._loop:
                self._ptt_active = False
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(self.bus.publish("ptt", active=False))
                )

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()

    def _start_mic(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            logger.warning("sounddevice missing — mic disabled")
            return

        sr = self.settings.sample_rate
        chunk = max(1, int(sr * self.settings.audio.chunk_ms / 1000))

        def callback(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                logger.debug("mic status: %s", status)
            mono = np.asarray(indata[:, 0] if indata.ndim > 1 else indata, dtype=np.float32)
            self._frames.append(mono.copy())

        self._stream = sd.InputStream(
            samplerate=sr,
            channels=1,
            dtype="float32",
            blocksize=chunk,
            device=self.settings.audio.input_device,
            callback=callback,
        )
        self._stream.start()

    async def _process_loop(self) -> None:
        while self._running:
            if not self._frames:
                await asyncio.sleep(0.01)
                continue
            frame = self._frames.popleft()
            await self._handle_frame(frame)

    async def _handle_frame(self, frame: np.ndarray) -> None:
        if self._finalizing:
            return

        rms = float(np.sqrt(np.mean(np.square(frame)) + 1e-12))
        is_voice = rms > 0.008
        now = time.time()

        if (
            self.settings.audio.wake_enabled
            and not self._listen_armed
            and not self._ptt_active
            and self.bus.state == SessionState.IDLE
            and self._wake is not None
        ):
            hit = await self._wake.check(frame)
            if hit:
                self._arm_listen("wake")
                return

        if (
            self.settings.audio.barge_in
            and self._speaking_ref
            and self._ptt_active
            and is_voice
            and rms > 0.05
        ):
            # Only an explicit PTT press may interrupt speech. Without acoustic
            # echo cancellation the microphone hears ADAM's own speaker output
            # and would otherwise cancel itself.
            await self.bus.publish("barge_in")
            await self.bus.publish("cancel")
            self._arm_listen("barge_in")

        if not self._listen_armed and not self._ptt_active:
            return

        self._utterance_buffer.append(frame)
        if is_voice:
            self._speech_started = True
            self._last_voice_ts = now

        silence_s = self.settings.audio.silence_ms / 1000.0
        max_s = self.settings.audio.max_utterance_ms / 1000.0
        buffered = sum(len(f) for f in self._utterance_buffer) / self.settings.sample_rate

        # Hold-to-talk: wait for key release
        if self._ptt_active:
            return

        # No speech after arm → idle (avoid stuck listening)
        if not self._speech_started and (now - self._armed_at) >= 4.0:
            logger.info("Listening timeout without speech")
            self._listen_armed = False
            self._utterance_buffer.clear()
            self.bus.set_state(SessionState.IDLE)
            return

        if self._speech_started and (now - self._last_voice_ts) >= silence_s:
            await self._finalize_utterance(reason="silence")
        elif buffered >= max_s:
            await self._finalize_utterance(reason="max_len")

    async def _finalize_utterance(self, reason: str) -> None:
        if self._finalizing:
            return
        self._finalizing = True
        try:
            self._listen_armed = False
            self._ptt_active = False
            self._speech_started = False

            if not self._utterance_buffer:
                if self.bus.state == SessionState.LISTENING:
                    self.bus.set_state(SessionState.IDLE)
                return

            audio = np.concatenate(self._utterance_buffer)
            self._utterance_buffer.clear()

            # Drop near-empty clips
            if audio.size < self.settings.sample_rate * 0.25:
                self.bus.set_state(SessionState.IDLE)
                return

            self.bus.set_state(SessionState.THINKING)
            await self.bus.publish("stt_start", reason=reason)

            assert self._stt is not None
            text = await self._stt.transcribe(audio)
            text = self._strip_wake(text)
            await self.bus.publish("stt", text=text, reason=reason)
            logger.info("STT (%s): %r", reason, text[:120] if text else "")

            if text:
                await self.bus.publish("utterance", text=text)
            else:
                await self.bus.publish("no_speech")
                self.bus.set_state(SessionState.IDLE)
        finally:
            self._finalizing = False

    def _strip_wake(self, text: str) -> str:
        out = text.strip()
        for phrase in self.settings.audio.wake_phrases:
            out = re.sub(rf"(?i)^\s*{re.escape(phrase)}[,.:]?\s*", "", out)
        return out.strip()


def _key_name(key: Any) -> str:
    try:
        from pynput.keyboard import Key

        if isinstance(key, Key):
            return key.name or str(key)
    except Exception:  # noqa: BLE001
        pass
    try:
        return key.char.lower() if key.char else str(key).lower()
    except Exception:  # noqa: BLE001
        return str(key).replace("'", "").lower()


class SpeechToText:
    """Prefer ElevenLabs Scribe; optional local faster-whisper fallback."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = None
        self.ready = False
        self.provider = "none"
        self._client = httpx.AsyncClient(timeout=60.0)

    async def load(self) -> None:
        provider = getattr(self.settings.stt, "provider", "elevenlabs")
        if provider in {"elevenlabs", "auto"} and self.settings.elevenlabs_api_key:
            self.provider = "elevenlabs"
            self.ready = True
            logger.info("STT provider: ElevenLabs Scribe")
            return

        if provider in {"whisper", "auto", "local"}:
            def _load():
                try:
                    from faster_whisper import WhisperModel

                    return WhisperModel(
                        self.settings.stt.model,
                        device=self.settings.stt.device,
                        compute_type=self.settings.stt.compute_type,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("faster-whisper unavailable: %s", exc)
                    return None

            try:
                self._model = await asyncio.to_thread(_load)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Whisper load failed: %s", exc)
                self._model = None
            if self._model is not None:
                self.provider = "whisper"
                self.ready = True
                return

        if self.settings.elevenlabs_api_key:
            self.provider = "elevenlabs"
            self.ready = True
            logger.info("STT fallback: ElevenLabs Scribe")
            return

        logger.error("No STT provider available")
        self.ready = False

    async def transcribe(self, audio: np.ndarray) -> str:
        if audio.size < self.settings.sample_rate * 0.2:
            return ""
        if self.provider == "elevenlabs":
            return await self._elevenlabs(audio)
        if self.provider == "whisper" and self._model is not None:
            return await self._whisper(audio)
        return ""

    async def _elevenlabs(self, audio: np.ndarray) -> str:
        wav = _to_wav_bytes(audio, self.settings.sample_rate)
        lang = (self.settings.stt.language or "").lower()
        data = {
            "model_id": getattr(self.settings.stt, "eleven_model", "scribe_v2"),
        }
        # ISO-639-1 preferred by Scribe when provided
        if lang:
            data["language_code"] = "es" if lang.startswith("es") else lang
        try:
            resp = await self._client.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": self.settings.elevenlabs_api_key},
                files={"file": ("utterance.wav", wav, "audio/wav")},
                data=data,
            )
            if resp.status_code >= 400:
                logger.error("ElevenLabs STT %s: %s", resp.status_code, resp.text[:300])
                return ""
            data = resp.json()
            return (data.get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.exception("ElevenLabs STT failed: %s", exc)
            return ""

    async def _whisper(self, audio: np.ndarray) -> str:
        def _run():
            try:
                segments, _info = self._model.transcribe(
                    audio,
                    language=self.settings.stt.language,
                    vad_filter=True,
                    beam_size=1,
                )
                return " ".join(seg.text.strip() for seg in segments).strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("whisper failed: %s", exc)
                return ""

        return await asyncio.to_thread(_run)


def _to_wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    import soundfile as sf

    buf = io.BytesIO()
    clipped = np.clip(audio, -1.0, 1.0)
    sf.write(buf, clipped, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class WakeWordEngine:
    """Optional openWakeWord; no aggressive energy false-triggers."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._oww = None
        self._cooldown = 0.0

    async def load(self) -> None:
        def _load():
            try:
                import openwakeword
                from openwakeword.model import Model

                openwakeword.utils.download_models()
                return Model(wakeword_models=["hey_jarvis"])
            except Exception as exc:  # noqa: BLE001
                logger.info("openWakeWord not active (%s); use PTT", exc)
                return None

        self._oww = await asyncio.to_thread(_load)

    async def check(self, frame: np.ndarray) -> bool:
        if self._oww is None:
            return False
        now = time.time()
        if now < self._cooldown:
            return False
        try:
            audio_i16 = (frame * 32767).astype(np.int16)
            preds = self._oww.predict(audio_i16)
            score = max(float(v) for v in preds.values()) if preds else 0.0
            if score >= self.settings.audio.wake_sensitivity:
                self._cooldown = now + 2.0
                return True
        except Exception:  # noqa: BLE001
            return False
        return False
