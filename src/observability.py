"""Logs locales y reporte final, sin servicios de observabilidad adicionales."""

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from livekit.agents import AgentSession, JobContext

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("clara")


def redact(text: str) -> str:
    for name in (
        "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "DEEPGRAM_API_KEY",
        "OPENAI_API_KEY", "ELEVEN_API_KEY", "RUNWAYML_API_SECRET",
    ):
        if value := os.getenv(name):
            text = text.replace(value, "[REDACTED]")
    return text


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if hasattr(record, "error"):
            text += " error=" + str(record.error)
        return redact(text)


def configure_logging() -> None:
    root = logging.getLogger()
    for existing in root.handlers:
        if not isinstance(existing.formatter, SafeFormatter):
            existing.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    path = ROOT / "outputs" / "agent.log"
    path.parent.mkdir(exist_ok=True)
    if not any(isinstance(h, logging.FileHandler) and h.baseFilename == str(path)
               for h in root.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)


def json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    return str(value)


class Observation:
    def __init__(self, session: AgentSession, config: dict) -> None:
        self.session = session
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        self.started_at = time.time()
        self.config = config
        self.session_started = False
        self.avatar_events: list[dict] = []
        self.interview_events: list[dict] = []
        self.component_metrics: list[dict] = []
        self.timeline_events: list[dict] = []
        self.audio_output = None
        self.interruption_evidence: list[dict] = []
        self.interruption_mode = "no observado"
        self.sdk_diagnostics: list[dict] = []
        self.diagnostic_handler = InterruptionDiagnostics(self)

    def log(self, event: str, data) -> None:
        if event.startswith("interview_"):
            self.interview_events.append({"event": event, "timestamp": time.time(), "data": data})
        if event.startswith("avatar_") or event == "session_start_failed":
            self.avatar_events.append({"event": event, "timestamp": time.time(), "data": data})
        logger.info("session=%s event=%s %s", self.id, event,
                    redact(json.dumps(data, default=json_value, ensure_ascii=False)))

    def timeline(self, event: str, timestamp: float, source: str, **data) -> None:
        entry = {"event": event, "timestamp": timestamp, "source": source, **data}
        self.timeline_events.append(entry)
        self.log(event, entry)

    def attach_audio_output(self) -> None:
        self.audio_output = self.session.output.audio
        if self.audio_output is None:
            return
        self.audio_output.on("playback_started", self.on_playback_started)
        self.audio_output.on("playback_finished", self.on_playback_finished)

    def detach_audio_output(self) -> None:
        if self.audio_output is None:
            return
        self.audio_output.off("playback_started", self.on_playback_started)
        self.audio_output.off("playback_finished", self.on_playback_finished)
        self.audio_output = None

    def on_playback_started(self, event) -> None:
        source = ("runway_first_audio_frame_forwarded" if self.config["runway_active"]
                  else "livekit_audio_output_playback_started")
        self.timeline("agent_playback_started", event.created_at, source)

    def on_playback_finished(self, event) -> None:
        now = time.time()
        source = ("runway_playback_finished_rpc_or_clear_buffer_timeout"
                  if self.config["runway_active"] else "livekit_audio_output_playback_finished")
        self.timeline("agent_playback_finished", now, source,
                      playback_position=event.playback_position, interrupted=event.interrupted)
        if event.interrupted:
            self.timeline("agent_interrupted", now, "audio_output_interrupted",
                          cause="unconfirmed")

    def effective_options(self) -> dict:
        options = self.session.options
        return {
            "endpointing": dict(options.endpointing),
            "interruption": {"mode": "auto", **options.interruption},
            "preemptive_generation": dict(options.preemptive_generation),
            "user_turn_limit": dict(options.turn_handling["user_turn_limit"]),
        }

    def attach(self, stt, llm, tts) -> None:
        logging.getLogger("livekit.agents").addHandler(self.diagnostic_handler)
        for component, provider in (("stt", stt), ("llm", llm), ("tts", tts)):
            def on_metrics(metric, component=component):
                data = {"component": component, "timestamp": time.time(),
                        "metrics": json_value(metric)}
                self.component_metrics.append(data)
                self.log("component_metrics", data)
                # El SDK emite estas métricas al finalizar; los inicios se reconstruyen
                # retrospectivamente con duration/ttft/ttfb, no son callbacks en vivo.
                if component == "llm":
                    self.timeline("llm_generation_started", metric.timestamp - metric.duration,
                                  "llm_metrics_reconstructed", request_id=metric.request_id)
                    if metric.ttft >= 0:
                        self.timeline("llm_first_text", metric.timestamp - metric.duration + metric.ttft,
                                      "llm_metrics_reconstructed", request_id=metric.request_id)
                    self.timeline("llm_generation_finished", metric.timestamp,
                                  "llm_metrics_reconstructed", request_id=metric.request_id,
                                  cancelled=metric.cancelled)
                elif component == "tts" and metric.ttfb >= 0:
                    self.timeline("tts_first_audio", metric.timestamp - metric.duration + metric.ttfb,
                                  "tts_metrics_reconstructed", request_id=metric.request_id,
                                  segment_id=metric.segment_id)
            provider.on("metrics_collected", on_metrics)

        for event_name in (
            "user_input_transcribed", "conversation_item_added", "agent_false_interruption",
            "overlapping_speech", "error", "close", "agent_state_changed",
            "user_state_changed",
            "speech_created", "user_transcription_timeout", "metrics_collected",
        ):
            def on_event(event, event_name=event_name):
                data = json_value(event)
                if event_name in ("overlapping_speech", "error"):
                    self.interruption_evidence.append({"event": event_name, "data": data})
                if event_name == "overlapping_speech":
                    self.interruption_mode = "adaptive observado"
                self.log(event_name, data)
                if event_name == "user_state_changed":
                    if event.new_state == "speaking":
                        self.timeline("user_speech_started", event.created_at,
                                      "user_state_changed")
                    elif event.old_state == "speaking":
                        self.timeline("user_speech_stopped", event.created_at,
                                      "user_state_changed")
                elif event_name == "user_input_transcribed" and event.is_final:
                    self.timeline("transcript_final", event.created_at,
                                  "user_input_transcribed", item_id=event.item_id)
                elif event_name == "conversation_item_added" and getattr(event.item, "role", None) == "user":
                    self.timeline("user_turn_committed", event.created_at,
                                  "conversation_item_added", item_id=event.item.id)
                elif event_name == "conversation_item_added" and getattr(event.item, "role", None) == "assistant":
                    self.timeline("agent_text_committed", event.created_at,
                                  "conversation_item_added", item_id=event.item.id,
                                  interrupted=event.item.interrupted)
                elif event_name == "speech_created":
                    self.timeline("agent_speech_created", event.created_at,
                                  "speech_created", speech_id=event.speech_handle.id,
                                  speech_source=event.source)
                elif event_name == "agent_false_interruption":
                    self.timeline("agent_false_interruption", event.created_at,
                                  "agent_false_interruption", resumed=event.resumed)
            self.session.on(event_name, on_event)
        self.log("session_start", {"mode": self.config["mode"], "config": self.config,
                                   "turn_handling": self.effective_options()})


class InterruptionDiagnostics(logging.Handler):
    """Conservar avisos de degradación del SDK sin acceder a su estado privado."""

    def __init__(self, observation: Observation) -> None:
        super().__init__()
        self.observation = observation
        self.setFormatter(SafeFormatter("%(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "interrupt" not in message.lower():
            return
        self.observation.sdk_diagnostics.append({"timestamp": record.created,
                                                 "message": self.format(record)})
        if "falling back to VAD" in message or "failed to create AdaptiveInterruptionDetector" in message:
            self.observation.interruption_mode = "vad (degradación registrada por SDK)"
        elif "adaptive interruption is disabled" in message:
            self.observation.interruption_mode = "vad (selección registrada por SDK)"


async def save_report(ctx: JobContext) -> None:
    if interview := ctx.proc.userdata.pop("clara_interview", None):
        await interview.aclose()
    if visual := ctx.proc.userdata.pop("clara_visual", None):
        await visual.aclose()
    observation = ctx.proc.userdata.pop("clara_observation", None)
    if not isinstance(observation, Observation):
        return
    observation.detach_audio_output()
    logging.getLogger("livekit.agents").removeHandler(observation.diagnostic_handler)
    ended_at = time.time()
    report = ctx.make_session_report().to_dict() if observation.session_started else {
        "startup_failed": True,
    }
    # Retener el reporte nativo y añadir únicamente el contexto local de la POC.
    report["poc"] = {
        "session_id": observation.id,
        "config": observation.config,
        "started_at": observation.started_at,
        "ended_at": ended_at,
        "duration_seconds": ended_at - observation.started_at,
        "turn_handling": observation.effective_options(),
        "component_metrics": observation.component_metrics,
        "timeline_events": observation.timeline_events,
        "interruption_evidence": observation.interruption_evidence,
        "interruption_mode": observation.interruption_mode,
        "sdk_diagnostics": observation.sdk_diagnostics,
        "avatar_events": observation.avatar_events,
        "interview_events": observation.interview_events,
    }
    session_id = re.sub(r"[^A-Za-z0-9_.-]", "_", observation.id)
    path = ROOT / "outputs" / session_id / "report.json"
    payload = redact(json.dumps(report, indent=2, ensure_ascii=False, default=json_value))

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    await asyncio.to_thread(write)
    observation.log("session_end", {"report": str(path), "ended_at": ended_at})
