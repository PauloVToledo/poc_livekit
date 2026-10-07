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
        self.component_metrics: list[dict] = []
        self.interruption_evidence: list[dict] = []
        self.interruption_mode = "no observado"
        self.sdk_diagnostics: list[dict] = []
        self.diagnostic_handler = InterruptionDiagnostics(self)

    def log(self, event: str, data) -> None:
        if event.startswith("avatar_") or event == "session_start_failed":
            self.avatar_events.append({"event": event, "timestamp": time.time(), "data": data})
        logger.info("session=%s event=%s %s", self.id, event,
                    redact(json.dumps(data, default=json_value, ensure_ascii=False)))

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
            provider.on("metrics_collected", on_metrics)

        for event_name in (
            "user_input_transcribed", "conversation_item_added", "agent_false_interruption",
            "overlapping_speech", "error", "close", "agent_state_changed",
            "user_state_changed",
        ):
            def on_event(event, event_name=event_name):
                data = json_value(event)
                if event_name in ("overlapping_speech", "error"):
                    self.interruption_evidence.append({"event": event_name, "data": data})
                if event_name == "overlapping_speech":
                    self.interruption_mode = "adaptive observado"
                self.log(event_name, data)
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
    if visual := ctx.proc.userdata.pop("clara_visual", None):
        await visual.aclose()
    observation = ctx.proc.userdata.pop("clara_observation", None)
    if not isinstance(observation, Observation):
        return
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
        "interruption_evidence": observation.interruption_evidence,
        "interruption_mode": observation.interruption_mode,
        "sdk_diagnostics": observation.sdk_diagnostics,
        "avatar_events": observation.avatar_events,
    }
    session_id = re.sub(r"[^A-Za-z0-9_.-]", "_", observation.id)
    path = ROOT / "outputs" / session_id / "report.json"
    payload = redact(json.dumps(report, indent=2, ensure_ascii=False, default=json_value))

    def write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    await asyncio.to_thread(write)
    observation.log("session_end", {"report": str(path), "ended_at": ended_at})
