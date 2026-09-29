from __future__ import annotations

import json
import logging
import re
from typing import Any

from aether_core.config import Settings
from aether_core.events import EventBus, SessionState

logger = logging.getLogger("aether.session")

_NAME_RE = re.compile(
    r"(?i)\b(?:me\s+llamo|soy|mi\s+nombre\s+es|ll[aá]mame)\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\wÁÉÍÓÚÜÑáéíóúüñ-]{1,30})"
)
_STATUS_RE = re.compile(
    r"(?i)^(oye\s+adam[, ]*)?("
    r"qué estás haciendo|que estas haciendo|"
    r"en qué vas|en que vas|"
    r"estado( de (los )?(trabajos|misiones|jobs))?|"
    r"qué misiones( hay)?|que misiones( hay)?|"
    r"jobs activos|cómo vas|como vas"
    r")\s*$"
)
_ACTION_RE = re.compile(
    r"(?i)\b("
    r"abre|abrir|ábrelo|abrelo|open|launch|lanza|lanzar|pon|poner|"
    r"ejecuta|ejecutar|corre|correr|run|inicia|iniciar|activa|activar|"
    r"cierra|cerrar|crea|crear|escribe|escribir|guarda|guardar|"
    r"sube|subir|carga|cargar|reproduce|reproducir|toca|escucha|"
    r"envía|envia|enviar|manda|mandar|instala|instalar|"
    r"mueve|mover|copia|copiar|borra|borrar|elimina|eliminar|"
    r"descarga|descargar|sube|subir|haz|realiza|realizar|"
    r"revisa|revisar|inspecciona|inspeccionar|"
    r"muéstrame|muestrame|dime\s+qué\s+procesos|"
    r"quiero\s+que|necesito\s+que"
    r")\b"
)


class SessionDirector:
    """Voice-turn orchestrator: transcript → Kimi plan → policy → jobs → TTS."""

    def __init__(
        self,
        settings: Settings,
        bus: EventBus,
        brain: Any,
        voice: Any,
        memory: Any,
        policy: Any,
        supervisor: Any,
        tool_registry: Any,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.brain = brain
        self.voice = voice
        self.memory = memory
        self.policy = policy
        self.supervisor = supervisor
        self.tools = tool_registry
        self._pending_confirm: dict[str, Any] | None = None
        self._history: list[dict[str, Any]] = []
        self._user_name = settings.memory.user_name or "Luisfer"

    async def start(self) -> None:
        await self.memory.ensure_identity(
            self.settings.memory.user_name,
            self.settings.memory.address_as,
        )
        prefs = await self.memory.list_preferences()
        self._user_name = (
            prefs.get("address_as")
            or prefs.get("user_name")
            or self.settings.memory.address_as
            or "Luisfer"
        )
        self._history = await self.memory.load_history(
            limit=self.settings.memory.session_turns * 2
        )
        self.bus.on("utterance", self._on_utterance)
        self.bus.on("cancel", self._on_cancel)
        self.bus.on("no_speech", self._on_no_speech)
        self.bus.on("cancel_job", self._on_cancel_job)
        self.bus.on("cancel_mission", self._on_cancel_mission)
        self.bus.on("list_jobs", self._on_list_jobs)
        self.bus.on("hud_connected", self._on_list_jobs)
        self.bus.on("mission_done", self._on_mission_done)
        self.bus.on("mission_error", self._on_mission_done)
        self.bus.on("mission_cancelled", self._on_mission_done)
        self.bus.on("mission_progress", self._on_mission_progress)
        await self.bus.publish(
            "session_ready", name=self.settings.name, user=self._user_name
        )
        logger.info(
            "Session ready for %s — %d history turns loaded",
            self._user_name,
            len(self._history),
        )

    async def _on_no_speech(self, _event: Any) -> None:
        await self.speak(f"No te escuché bien, {self._user_name}. Habla de nuevo.")
        self.bus.set_state(SessionState.IDLE)

    async def _on_cancel(self, event: Any) -> None:
        await self.supervisor.cancel_all()
        self._pending_confirm = None
        self.bus.set_state(SessionState.IDLE)
        await self.speak("Cancelado.")

    async def _on_cancel_job(self, event: Any) -> None:
        job_id = str(event.payload.get("job_id") or "")
        cancelled = await self.supervisor.cancel_job(job_id) if job_id else False
        if cancelled:
            await self.speak("Cancelé ese trabajo.")
        await self._publish_jobs_snapshot()

    async def _on_cancel_mission(self, event: Any) -> None:
        mission_id = str(event.payload.get("mission_id") or "")
        if mission_id:
            await self.supervisor.cancel_mission(mission_id)
        await self._publish_jobs_snapshot()

    async def _on_list_jobs(self, _event: Any) -> None:
        await self._publish_jobs_snapshot()
        await self._publish_integrations()
        await self._publish_routines()

    async def _publish_jobs_snapshot(self) -> None:
        snapshot = await self.supervisor.snapshot()
        await self.bus.publish("jobs_snapshot", **snapshot)

    async def _publish_integrations(self) -> None:
        if self.tools.get("integration.status") is None:
            return
        try:
            raw = await self.tools.call("integration.status", {"service": "all"})
            data = json.loads(raw) if isinstance(raw, str) else raw
            await self.bus.publish("integrations", **data)
        except Exception:  # noqa: BLE001
            logger.exception("integration status failed")

    async def _publish_routines(self) -> None:
        if self.tools.get("routine.list") is None:
            return
        try:
            raw = await self.tools.call("routine.list", {})
            data = json.loads(raw) if isinstance(raw, str) else raw
            await self.bus.publish("routines", **data)
        except Exception:  # noqa: BLE001
            logger.exception("routine list failed")

    async def _on_mission_progress(self, event: Any) -> None:
        payload = event.payload or {}
        await self.bus.publish(
            "job_progress",
            id=payload.get("job_id") or payload.get("id"),
            tool="cursor.delegate",
            label=str(payload.get("phase") or "Cursor"),
            progress=0.55,
        )

    async def _on_mission_done(self, event: Any) -> None:
        if self.bus.state in {
            SessionState.LISTENING,
            SessionState.SPEAKING,
            SessionState.THINKING,
            SessionState.ACTING,
            SessionState.AWAITING_CONFIRM,
        }:
            return
        result = event.payload.get("result") or {}
        if event.type == "mission_cancelled":
            text = "Cancelé la misión de Cursor."
        elif result.get("ok") is True and result.get("verified") is not False:
            text = str(
                result.get("message")
                or result.get("summary")
                or "Cursor terminó y verifiqué el resultado."
            )[:240]
        else:
            text = f"La misión de Cursor falló: {result.get('error', 'sin evidencia')}."
        await self.speak(text)
        self.bus.set_state(SessionState.IDLE)
        await self._publish_jobs_snapshot()

    async def _on_utterance(self, event: Any) -> None:
        text = (event.payload.get("text") or "").strip()
        if not text:
            return

        await self._maybe_learn_name(text)

        if self._is_cancel_command(text):
            await self._on_cancel(event)
            return

        if self._pending_confirm is not None:
            await self._handle_confirmation(text)
            return

        if self._is_status_query(text):
            await self._speak_status()
            return

        await self._run_turn(text)

    @staticmethod
    def _is_cancel_command(text: str) -> bool:
        """Only exact cancel intents — NEVER match Spanish 'para' inside normal sentences."""
        t = text.strip().lower()
        t = re.sub(r"[¿?¡!.…,]+", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        exact = {
            "cancela",
            "cancelar",
            "cancelado",
            "cancel",
            "stop",
            "detener",
            "detente",
            "para todo",
            "para adam",
            "oye adam para",
            "adam para",
            "olvídalo",
            "olvidalo",
            "no importa",
        }
        if t in exact:
            return True
        # Short dedicated phrases only
        return bool(
            re.fullmatch(
                r"(cancela(r|do)?( eso| todo)?|stop|detener(te)?|para todo|basta ya)",
                t,
            )
        )

    @staticmethod
    def _is_status_query(text: str) -> bool:
        t = re.sub(r"[¿?¡!.…,]+", " ", text.strip().lower())
        t = re.sub(r"\s+", " ", t).strip()
        return bool(_STATUS_RE.fullmatch(t))

    async def _speak_status(self) -> None:
        snapshot = await self.supervisor.snapshot()
        await self._publish_jobs_snapshot()
        jobs = list(snapshot.get("jobs") or [])
        for mission in snapshot.get("missions") or []:
            if isinstance(mission, dict):
                jobs.append(
                    {
                        "status": mission.get("status"),
                        "label": mission.get("title") or mission.get("prompt") or "Cursor",
                    }
                )
        active = [
            job
            for job in jobs
            if str(job.get("status") or "")
            not in {"done", "error", "cancelled", "completed"}
        ]
        if not active:
            text = f"Estoy en {self.bus.state.value}, {self._user_name}. No hay trabajos en curso."
        else:
            labels = [
                str(job.get("label") or job.get("tool") or job.get("title") or "misión")
                for job in active[:4]
            ]
            text = f"Tengo {len(active)} trabajo(s) activo(s): {', '.join(labels)}."
        await self.speak(text)
        self.bus.set_state(SessionState.IDLE)

    @staticmethod
    def _destination_from_args(arguments: dict[str, Any]) -> str:
        for key in (
            "to",
            "recipient",
            "entity_id",
            "chat_id",
            "path",
            "destination",
            "url",
            "draft_id",
        ):
            value = arguments.get(key)
            if value:
                return f"{key} {value}"
        return ""

    async def _maybe_learn_name(self, text: str) -> None:
        match = _NAME_RE.search(text)
        if not match:
            return
        name = match.group(1).strip().capitalize()
        if name.lower() in {"adam", "aether", "tú", "tu", "yo"}:
            return
        self._user_name = name
        await self.memory.ensure_identity(name, name)
        logger.info("Learned user name: %s", name)

    async def _handle_confirmation(self, text: str) -> None:
        pending = self._pending_confirm
        self._pending_confirm = None
        affirm = any(
            w in text.lower()
            for w in ("sí", "si", "confirma", "adelante", "yes", "ok", "dale")
        )
        if not affirm:
            self.bus.set_state(SessionState.IDLE)
            await self.speak(f"Entendido, {self._user_name}. No lo hago.")
            return

        assert pending is not None
        self.bus.set_state(SessionState.ACTING)
        result = await self.supervisor.run_tool(
            pending["name"], pending["arguments"], force=True
        )
        await self.speak(self._summarize_result(pending["name"], result))
        self.bus.set_state(SessionState.IDLE)

    async def _run_turn(self, text: str) -> None:
        self.bus.set_state(SessionState.THINKING)
        await self.bus.publish("transcript", text=text, role="user")
        await self.memory.add_episode("user", text)

        memories = await self.memory.recall(text, limit=10)
        tools = self.tools.openai_tools()
        messages = self._build_messages(text, memories)
        requires_action = self._is_action_request(text)

        try:
            final_text, tool_trace = await self.brain.run_agent_loop(
                messages,
                tools,
                execute_tool=self._execute_tool_with_policy,
                require_tool=requires_action,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("brain failure")
            await self.speak(f"Falló el razonamiento: {exc}")
            self.bus.set_state(SessionState.IDLE)
            return

        if self._pending_confirm is not None:
            return

        trace_failure = self._first_failed_tool(tool_trace)
        if trace_failure is not None:
            reply = self._summarize_result(
                trace_failure.get("name", "acción"),
                trace_failure.get("result"),
            )
        elif requires_action and not tool_trace:
            reply = (
                f"No ejecuté esa acción, {self._user_name}: "
                "ninguna herramienta confirmó el resultado."
            )
        elif requires_action and tool_trace:
            # Never let free-form model text narrate an action. Spoken claims
            # come only from the last concrete tool result.
            last = tool_trace[-1]
            reply = self._summarize_result(
                last.get("name", "acción"),
                last.get("result"),
            )
        else:
            reply = (final_text or self._fallback_from_trace(tool_trace)).strip()
        if not reply:
            reply = f"Listo, {self._user_name}."
        await self.memory.add_episode("assistant", reply)
        self._history.append({"role": "user", "content": text})
        self._history.append({"role": "assistant", "content": reply})
        self._trim_history()
        await self.speak(reply)
        self.bus.set_state(SessionState.IDLE)

    def _build_messages(self, text: str, memories: list[str]) -> list[dict[str, Any]]:
        mem_block = "\n".join(f"- {m}" for m in memories) or "- (sin memorias)"
        user = self._user_name
        ident = self.settings.identity
        system = (
            f"Eres {self.settings.name}. Tu ÚNICO nombre es ADAM. "
            "Nunca digas Aether, AETHER ni éter. Si el historial menciona ese nombre viejo, ignóralo. "
            f"Eres el asistente de voz de {user}: "
            "desarrollador automatizado y gestor de activos del PC. "
            f"Dirígete SIEMPRE a él como {user} (no digas 'usuario'). "
            "Cuando te saluden, responde como ADAM (ej: 'Hola Luisfer, ADAM en línea'). "
            "Responde SIEMPRE en español, frases cortas aptas para TTS (máx 2 oraciones "
            "salvo que pida detalle). No digas que eres un chat. "
            "IMPORTANTE sobre tu nombre: solo si te preguntan explícitamente qué significa "
            f"ADAM / A.D.A.M. / tu nombre, explica que es '{ident.expansion_es}' "
            f"(en inglés: '{ident.expansion_en}'). "
            "Si no te lo preguntan, NO digas el significado ni deletrees las siglas. "
            "Tienes permiso total del dueño para operar este PC y para abrir Cursor "
            "y enviarle prompts al agente del proyecto (tools cursor_open_project / cursor_send_agent). "
            "NO pidas permiso ni confirmación para operaciones normales; ejecuta tools de inmediato. "
            "REGLA CRÍTICA: ante una orden, debes llamar herramientas. Nunca digas 'listo', "
            "'abierto', 'hecho' o equivalentes sin un resultado de tool con ok=true. "
            "Si una tool falla o no verifica el efecto, informa el fallo exacto. "
            "Para trabajo de desarrollo complejo usa cursor.send_agent o agent.delegate_developer; "
            "ADAM la dejará en segundo plano y avisará al terminar. "
            "Para UI nativa usa vision.act_and_verify o desktop.click_target; "
            "para web usa browser.* con verificación. "
            "Para archivos usa files.search / files.move / files.trash; "
            "para salud del PC health.snapshot; para backups backup.run; "
            "para mensajería crea borrador y solo envía tras confirmación. "
            "Para reproducir en YouTube usa browser.youtube_play; para subir carpetas a Drive "
            "usa browser.drive_upload; para WhatsApp de escritorio usa desktop.open_whatsapp; "
            "para Spotify usa desktop.spotify_play o spotify.control si hay OAuth. "
            "Casa: usa home.list_devices y home.set_device. Las luces locales son "
            "input_boolean.luz_oficina, input_boolean.luz_sala e input_boolean.luz_cuarto "
            "(también aceptan el nombre oficina/sala/cuarto). Verifica el estado. "
            "Ejecuta el objetivo completo, no te limites a abrir Google o una aplicación genérica. "
            "Si integration.status dice stub o needs_oauth, dilo: no inventes que ya se envió. "
            "Para conectar un servicio usa integration.onboard y nunca pidas ni escribas contraseñas. "
            "Si te preguntan qué estás haciendo, responde con el estado de jobs/misiones. "
            "Solo el sistema te bloqueará acciones realmente peligrosas. "
            "Recuerda el historial y las preferencias. "
            "Cuando hables de unidades, di las palabras completas "
            "(megabit, megabyte, gigabyte), nunca siglas crudas como mgb o mb. "
            f"Memoria relevante:\n{mem_block}"
        )
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(self._history[-(self.settings.memory.session_turns) :])
        messages.append({"role": "user", "content": text})
        return messages

    async def _execute_tool_with_policy(
        self, name: str, arguments: dict[str, Any]
    ) -> str:
        name = self.tools.resolve_name(name)
        decision = self.policy.evaluate(name, arguments)
        await self.bus.publish(
            "policy",
            tool=name,
            level=decision.level,
            allowed=decision.allowed,
            needs_confirm=decision.needs_confirm,
            reason=decision.reason,
        )

        if not decision.allowed:
            return json.dumps({"ok": False, "error": decision.reason})

        if decision.needs_confirm:
            destination = self._destination_from_args(arguments)
            self._pending_confirm = {"name": name, "arguments": arguments}
            self.bus.set_state(SessionState.AWAITING_CONFIRM)
            dest_bit = f" Destino: {destination}." if destination else ""
            await self.bus.publish(
                "policy",
                tool=name,
                level=decision.level,
                allowed=decision.allowed,
                needs_confirm=True,
                reason=decision.reason,
                destination=destination,
            )
            await self.speak(
                f"{self._user_name}, esto es sensible: {decision.reason}.{dest_bit} ¿Lo confirmas?"
            )
            self.bus.set_state(SessionState.AWAITING_CONFIRM)
            return json.dumps(
                {
                    "ok": False,
                    "pending_confirm": True,
                    "message": "Esperando confirmación oral del usuario.",
                }
            )

        if name in {"cursor.send_agent", "agent.delegate_developer"}:
            prompt = str(
                arguments.get("prompt")
                or arguments.get("task")
                or arguments.get("message")
                or ""
            ).strip()
            path = str(arguments.get("path") or arguments.get("cwd") or ".")
            verify_cmd = str(
                arguments.get("verify_command")
                or self.settings.supervisor.default_verify_command
                or ""
            ).strip()
            self.bus.set_state(SessionState.DELEGATING)
            mission_id = await self.supervisor.submit_mission(
                prompt,
                cwd=path,
                model=str(arguments.get("model") or "composer-2.5"),
                verification_commands=[verify_cmd] if verify_cmd else (),
            )
            return json.dumps(
                {
                    "ok": True,
                    "verified": True,
                    "background": True,
                    "mission_id": mission_id,
                    "message": (
                        f"Delegué la misión a Cursor. Te aviso cuando termine, {self._user_name}."
                    ),
                },
                ensure_ascii=False,
            )

        self.bus.set_state(SessionState.ACTING)
        result = await self.supervisor.run_tool(name, arguments)
        return result if isinstance(result, str) else json.dumps(result)

    @staticmethod
    def _is_action_request(text: str) -> bool:
        return bool(_ACTION_RE.search(text))

    @staticmethod
    def _first_failed_tool(trace: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in trace:
            result = item.get("result")
            if isinstance(result, str):
                try:
                    parsed = json.loads(result)
                except json.JSONDecodeError:
                    return item
            elif isinstance(result, dict):
                parsed = result
            else:
                return item
            if not isinstance(parsed, dict):
                return item
            if parsed.get("ok") is not True or parsed.get("verified") is False:
                return item
        return None

    async def speak(self, text: str) -> None:
        self.bus.set_state(SessionState.SPEAKING)
        await self.bus.publish("transcript", text=text, role="assistant")
        await self.voice.speak(text)

    def _trim_history(self) -> None:
        max_turns = self.settings.memory.session_turns * 2
        if len(self._history) > max_turns:
            self._history = self._history[-max_turns:]

    @staticmethod
    def _summarize_result(name: str, result: Any) -> str:
        if isinstance(result, str):
            try:
                data = json.loads(result)
            except json.JSONDecodeError:
                return result[:240]
        else:
            data = result
        if isinstance(data, dict):
            if data.get("ok") is not True or data.get("verified") is False:
                return f"Falló {name}: {data.get('error', 'resultado no verificado')}"
            if "message" in data:
                return str(data["message"])[:240]
            if "summary" in data:
                return str(data["summary"])[:240]
            return f"{name} terminó sin evidencia descriptiva."
        return f"{name} devolvió un resultado no verificable."

    @staticmethod
    def _fallback_from_trace(trace: list[dict[str, Any]]) -> str:
        if not trace:
            return "Listo."
        last = trace[-1]
        return SessionDirector._summarize_result(last.get("name", "acción"), last.get("result"))
