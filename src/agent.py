"""Un AgentServer local y una AgentSession por conversación."""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent, AgentServer, AgentSession, JobContext, ModelSettings, StopResponse,
    TurnHandlingOptions, inference, llm,
)
from livekit.agents.voice.room_io import RoomOptions
from livekit.plugins import deepgram, elevenlabs, openai, runway

from observability import Observation, configure_logging, save_report

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)
logger = logging.getLogger("clara")
AVATAR_START_TIMEOUT_SECONDS = 60
AVATAR_CLOSE_TIMEOUT_SECONDS = 5
INTERVIEW_TURN_GRACE_SECONDS = 30
INTERVIEW_INTRODUCTION = (
    "Hola, soy Clara de Datta. Gracias por participar en esta entrevista. "
    "Durante los próximos minutos me gustaría conocer un poco sobre tu experiencia "
    "en desarrollo de software. Para comenzar, cuéntame sobre un proyecto reciente "
    "en el que hayas trabajado."
)
INTERVIEW_CLOSING = (
    "Perfecto, con esto terminamos la entrevista. Muchas gracias por tu tiempo "
    "y por compartir tu experiencia. Ha sido un gusto conversar contigo."
)


def env_bool(name: str, default: str = "true") -> bool:
    value = os.getenv(name, default).strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} debe ser true o false")
    return value == "true"


def execution_mode() -> str:
    # lk 2.18 ejecuta python -m livekit.agents console o start --dev.
    if "console" in sys.argv[1:]:
        return "console"
    if "--dev" in sys.argv[1:]:
        return "dev"
    return "start"


def load_config(mode: str) -> dict:
    auto_start = env_bool("INTERVIEW_AUTO_START")
    auto_close = env_bool("INTERVIEW_AUTO_CLOSE")
    try:
        duration = int(os.getenv("INTERVIEW_DURATION_SECONDS", "300"))
    except ValueError:
        raise ValueError("INTERVIEW_DURATION_SECONDS debe ser un entero positivo") from None
    if duration <= 0:
        raise ValueError("INTERVIEW_DURATION_SECONDS debe ser un entero positivo")
    enabled = os.getenv("RUNWAY_ENABLED", "false").strip().lower()
    if enabled not in ("true", "false"):
        raise ValueError("RUNWAY_ENABLED debe ser true o false")
    active = enabled == "true" and mode != "console"
    avatar_id = os.getenv("RUNWAY_AVATAR_ID", "").strip()
    preset_id = os.getenv("RUNWAY_PRESET_ID", "").strip()
    max_duration = 900
    if active:
        if bool(avatar_id) == bool(preset_id):
            raise ValueError("Configura exactamente uno: RUNWAY_AVATAR_ID o RUNWAY_PRESET_ID")
        try:
            max_duration = int(os.getenv("RUNWAY_MAX_DURATION_SECONDS", "900"))
        except ValueError:
            raise ValueError("RUNWAY_MAX_DURATION_SECONDS debe ser un entero positivo") from None
        if max_duration <= 0:
            raise ValueError("RUNWAY_MAX_DURATION_SECONDS debe ser un entero positivo")
    required = ["DEEPGRAM_API_KEY", "OPENAI_API_KEY", "ELEVEN_API_KEY", "ELEVEN_VOICE_ID"]
    if mode != "console":
        required += ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"]
    if active:
        required += ["RUNWAYML_API_SECRET"]
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("Faltan variables en .env o en el entorno: " + ", ".join(missing))
    return {
        "mode": mode,
        "interview_auto_start": auto_start,
        "interview_auto_close": auto_close,
        "interview_duration_seconds": duration,
        "interview_turn_grace_seconds": INTERVIEW_TURN_GRACE_SECONDS,
        "deepgram_model": os.getenv("DEEPGRAM_MODEL") or "nova-3",
        "deepgram_language": os.getenv("DEEPGRAM_LANGUAGE") or "multi",
        "openai_model": os.getenv("OPENAI_MODEL") or "gpt-4.1-mini",
        "eleven_model": os.getenv("ELEVEN_MODEL") or "eleven_flash_v2_5",
        "eleven_voice_id": os.environ["ELEVEN_VOICE_ID"].strip(),
        "turn_detector": "v1-mini",
        "runway_enabled": enabled == "true",
        "runway_active": active,
        "runway_avatar_id": avatar_id if active else None,
        "runway_preset_id": preset_id if active else None,
        "runway_max_duration_seconds": max_duration if active else None,
        "runway_start_timeout_seconds": AVATAR_START_TIMEOUT_SECONDS if active else None,
    }


class Clara(Agent):
    def __init__(self, interview: "InterviewControl") -> None:
        super().__init__(instructions=(ROOT / "src" / "clara_prompt.txt").read_text(encoding="utf-8"))
        self.interview = interview

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        if self.interview.block_normal_reply:
            raise StopResponse()

    def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings):
        # También impedir generación anticipada nueva tras el deadline.
        if self.interview.block_normal_reply:
            return None
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)


class InterviewControl:
    """Inicio y cierre de testing, por sesión y usando APIs públicas del SDK."""

    def __init__(self, ctx: JobContext, observation: Observation) -> None:
        self.ctx = ctx
        self.observation = observation
        self.session = observation.session
        self.config = observation.config
        self.started_at: float | None = None
        self.ready = False
        self.stopped = False
        self.closing = False
        self.finished = False
        self.introduction_requested = False
        self.timer_task: asyncio.Task | None = None
        self.introduction_task: asyncio.Task | None = None
        self.audio_output = None
        self.session.on("close", self.on_close)
        self.session.on("conversation_item_added", self.on_item)
        self.ctx.room.on("participant_disconnected", self.on_participant_left)
        ctx.add_shutdown_callback(self.aclose)

    @property
    def elapsed_seconds(self) -> float:
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at

    @property
    def block_normal_reply(self) -> bool:
        return (
            self.stopped or self.closing
            or (self.config["interview_auto_start"] and not self.introduction_requested)
            or (self.config["interview_auto_close"] and self.started_at is not None
                and self.elapsed_seconds >= self.config["interview_duration_seconds"])
        )

    def begin(self, evidence: str) -> None:
        if not self.ready or self.stopped or self.started_at is not None:
            return
        self.started_at = time.monotonic()
        self.observation.log("interview_started", {"evidence": evidence})
        self.timer_task = asyncio.create_task(self.run_timer(), name="clara_interview_timer")

    def on_playback_started(self, event) -> None:
        self.begin("audio_output_playback_started")

    def on_item(self, event) -> None:
        # El modo texto no tiene playback; usar el primer mensaje de Clara.
        if isinstance(event.item, llm.ChatMessage) and event.item.role == "assistant" and (
            self.session.output.audio is None or not self.session.output.audio_enabled
        ):
            self.begin("assistant_text_committed_no_audio")

    def start(self) -> None:
        if self.ready or self.stopped:
            return
        self.ready = True
        self.audio_output = self.session.output.audio
        if self.audio_output is not None:
            self.audio_output.on("playback_started", self.on_playback_started)
        if self.config["interview_auto_start"]:
            self.introduction_requested = True
            speech = self.session.say(INTERVIEW_INTRODUCTION)
            self.introduction_task = asyncio.create_task(
                self.check_introduction(speech), name="clara_interview_introduction"
            )

    async def check_introduction(self, speech) -> None:
        await speech.wait_for_playout()
        if error := speech.exception():
            self.observation.log("interview_failed", {"phase": "introduction", "error": str(error)})
            self.session.shutdown(drain=True)

    async def run_timer(self) -> None:
        try:
            duration = self.config["interview_duration_seconds"]
            while self.elapsed_seconds < duration:
                await asyncio.sleep(min(30, duration - self.elapsed_seconds))
                self.observation.log("interview_elapsed_seconds", {"seconds": self.elapsed_seconds})
            self.observation.log("interview_timeout_reached", {"seconds": self.elapsed_seconds})
            if not self.config["interview_auto_close"]:
                return
            self.closing = True
            try:
                # Incluye endpointing/STT pendiente, no solo silencio del VAD.
                await asyncio.wait_for(
                    self.session.wait_for_idle(), timeout=INTERVIEW_TURN_GRACE_SECONDS
                )
            except TimeoutError:
                self.observation.log("interview_turn_grace_exceeded", {"seconds": self.elapsed_seconds})
                # El timeout cancela la espera, nunca el speech/TTS en curso.
                if speech := self.session.current_speech:
                    await speech.wait_for_playout()
            if self.stopped:
                return
            self.observation.log("interview_closing", {"seconds": self.elapsed_seconds})
            speech = self.session.say(INTERVIEW_CLOSING, allow_interruptions=False)
            await speech.wait_for_playout()
            if error := speech.exception():
                raise RuntimeError("Falló el TTS del cierre") from error
            if speech.interrupted:
                raise RuntimeError("El playback del cierre fue interrumpido")
            self.finish("closing_playback_completed")
            self.session.shutdown(drain=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.observation.log("interview_failed", {"phase": "timer_or_closing", "error": str(error)})
            self.session.shutdown(drain=True)

    def finish(self, reason: str) -> None:
        if self.started_at is not None and not self.finished:
            self.finished = True
            self.observation.log("interview_finished", {"seconds": self.elapsed_seconds, "reason": reason})

    def stop(self, reason: str) -> None:
        self.stopped = True
        self.finish(reason)
        for task in (self.timer_task, self.introduction_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()

    def on_close(self, event) -> None:
        self.stop("session_closed")
        self.ctx.shutdown(reason="clara_session_closed")

    def on_participant_left(self, participant) -> None:
        if participant.kind in (
            rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
            rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        ):
            self.stop("candidate_disconnected")

    async def aclose(self) -> None:
        self.stop("job_shutdown")
        self.session.off("close", self.on_close)
        self.session.off("conversation_item_added", self.on_item)
        self.ctx.room.off("participant_disconnected", self.on_participant_left)
        if self.audio_output is not None:
            self.audio_output.off("playback_started", self.on_playback_started)
        tasks = [task for task in (self.timer_task, self.introduction_task)
                 if task is not None and task is not asyncio.current_task()]
        await asyncio.gather(*tasks, return_exceptions=True)


class RunwayVisual:
    """Supervisar el avatar usando únicamente APIs públicas de LiveKit."""

    def __init__(self, ctx: JobContext, observation: Observation) -> None:
        self.ctx = ctx
        self.observation = observation
        config = observation.config
        self.avatar = runway.AvatarSession(
            avatar_id=config["runway_avatar_id"], preset_id=config["runway_preset_id"],
            max_duration=config["runway_max_duration_seconds"],
        )
        self.ready = asyncio.Event()
        self.video_task: asyncio.Task | None = None
        self.closing = False
        self.stopping = False
        self.failure_reason: str | None = None
        self.close_lock = asyncio.Lock()
        self.started_at = 0.0
        self.listeners = {
            "track_subscribed": self.on_track,
            "participant_disconnected": self.on_participant_left,
            "track_unpublished": self.on_track_lost,
            "track_unsubscribed": self.on_track_unsubscribed,
        }
        for event, callback in self.listeners.items():
            ctx.room.on(event, callback)
        observation.session.on("close", self.on_session_close)
        ctx.add_shutdown_callback(self.aclose)

    def fail(self, reason: str) -> None:
        if not self.closing and not self.stopping:
            self.failure_reason = reason
            self.stopping = True
            self.ready.set()
            self.observation.log("avatar_failure", {"reason": reason})
            self.ctx.shutdown(reason=reason)

    def on_session_close(self, event) -> None:
        self.stopping = True
        self.ctx.shutdown(reason="clara_session_closed")

    def on_participant_left(self, participant) -> None:
        if participant.identity == self.avatar.avatar_identity:
            self.fail("runway_participant_disconnected")
        elif participant.kind in (
            rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
            rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        ):
            self.failure_reason = "candidate_disconnected"
            self.stopping = True
            self.ready.set()
            self.observation.log("session_shutdown_requested", {"reason": self.failure_reason})
            self.ctx.shutdown(reason=self.failure_reason)

    def on_track_lost(self, publication, participant) -> None:
        if (participant.identity == self.avatar.avatar_identity
                and publication.kind == rtc.TrackKind.KIND_VIDEO):
            self.fail("runway_video_lost")

    def on_track_unsubscribed(self, track, publication, participant) -> None:
        self.on_track_lost(publication, participant)

    def on_track(self, track, publication, participant) -> None:
        if (participant.identity == self.avatar.avatar_identity
                and track.kind == rtc.TrackKind.KIND_VIDEO and self.video_task is None
                and not self.closing):
            self.video_task = asyncio.create_task(self.wait_first_frame(track))

    async def wait_first_frame(self, track) -> None:
        stream = rtc.VideoStream(track, capacity=1)
        try:
            async for _ in stream:
                self.observation.log("avatar_video_available", {
                    "identity": self.avatar.avatar_identity,
                    "startup_seconds": time.monotonic() - self.started_at,
                    "evidence": "first_video_frame_received",
                })
                self.ready.set()
                break
            if not self.ready.is_set():
                self.fail("runway_video_stream_ended_before_first_frame")
        except Exception as error:
            self.observation.log("avatar_failure", {"reason": "video_stream", "error": str(error)})
            self.fail("runway_video_stream_error")
        finally:
            await stream.aclose()

    async def start(self) -> None:
        self.started_at = time.monotonic()
        self.observation.log("avatar_start_requested", {"identity": self.avatar.avatar_identity})
        async with asyncio.timeout(AVATAR_START_TIMEOUT_SECONDS):
            await self.avatar.start(self.observation.session, room=self.ctx.room)
            self.observation.log("avatar_start_accepted", {
                "elapsed_seconds": time.monotonic() - self.started_at,
            })
            # También inspeccionar pistas que llegaron durante la solicitud HTTP.
            for participant in self.ctx.room.remote_participants.values():
                for publication in participant.track_publications.values():
                    if publication.track is not None:
                        self.on_track(publication.track, publication, participant)
            await self.ready.wait()
            if self.failure_reason:
                raise RuntimeError(self.failure_reason)

    async def aclose(self) -> None:
        async with self.close_lock:
            if self.closing:
                return
            self.closing = True
            self.stopping = True
            self.observation.log("avatar_close_requested", {"provider_final_status": "UNKNOWN"})
            for event, callback in self.listeners.items():
                self.ctx.room.off(event, callback)
            self.observation.session.off("close", self.on_session_close)
            if self.video_task is not None:
                self.video_task.cancel()
                await asyncio.gather(self.video_task, return_exceptions=True)
            try:
                await asyncio.wait_for(self.avatar.aclose(), timeout=AVATAR_CLOSE_TIMEOUT_SECONDS)
                self.observation.log("avatar_close_method_returned", {"provider_final_status": "UNKNOWN"})
            except Exception as error:
                self.observation.log("avatar_close_failed", {
                    "error_type": type(error).__name__, "error": str(error),
                    "provider_final_status": "UNKNOWN",
                })


# Validar antes de arrancar el worker; la importación de herramientas de inspección
# y tests no inicia conexiones ni necesita credenciales.
if any(command in sys.argv[1:] for command in ("console", "start")):
    configure_logging()
    try:
        logger.info("Configuración: %s", load_config(execution_mode()))
    except ValueError as error:
        logger.error("%s", error)
        raise

server = AgentServer()


@server.rtc_session(agent_name="clara-livekit-poc", on_session_end=save_report)
async def clara_session(ctx: JobContext) -> None:
    configure_logging()
    config = load_config(execution_mode())
    stt = deepgram.STT(model=config["deepgram_model"], language=config["deepgram_language"])
    llm = openai.responses.LLM(model=config["openai_model"])
    tts = elevenlabs.TTS(model=config["eleven_model"], voice_id=config["eleven_voice_id"])
    session = AgentSession(
        stt=stt, llm=llm, tts=tts,
        turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector(version="v1-mini")),
    )
    observation = Observation(session, config)
    ctx.proc.userdata["clara_observation"] = observation
    observation.attach(stt, llm, tts)
    interview = InterviewControl(ctx, observation)
    ctx.proc.userdata["clara_interview"] = interview
    try:
        if config["runway_active"]:
            await ctx.connect()
            visual = RunwayVisual(ctx, observation)
            ctx.proc.userdata["clara_visual"] = visual
            await visual.start()
        options = {}
        if config["runway_active"]:
            # Runway entra como AGENT; nunca debe convertirse en el candidato.
            options["room_options"] = RoomOptions(participant_kinds=[
                rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
                rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
            ])
        await session.start(agent=Clara(interview), room=ctx.room, **options)
        observation.session_started = True
        if not config["runway_active"]:
            await ctx.connect()
        if config["mode"] != "console":
            await session.room_io.wait_for_ready()
        observation.log("session_ready", {"turn_handling": observation.effective_options()})
        interview.start()
    except (Exception, asyncio.CancelledError) as error:
        observation.log("session_start_failed", {
            "error_type": type(error).__name__, "error": str(error),
        })
        await interview.aclose()
        if visual := ctx.proc.userdata.get("clara_visual"):
            await visual.aclose()
        ctx.shutdown(reason="clara_start_failed")
        logger.exception("session=%s Falló el inicio de la conversación", observation.id)
        raise
