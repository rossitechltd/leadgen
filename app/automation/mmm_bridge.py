"""UDP + file bridge for Mini Mouse Macro integration."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.automation.signals import MmmCommand, MmmSignal

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class BridgeStatus:
    phase: str = "idle"
    mmm_signal: str | None = None
    last_command: str | None = None
    last_signal_at: str | None = None
    last_command_at: str | None = None
    message: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "mmm_signal": self.mmm_signal,
            "last_command": self.last_command,
            "last_signal_at": self.last_signal_at,
            "last_command_at": self.last_command_at,
            "message": self.message,
            "extra": self.extra,
        }


class MmmBridge:
    """Send commands to MMM and receive status via UDP and/or files."""

    def __init__(
        self,
        *,
        coord_dir: Path,
        udp_host: str = "127.0.0.1",
        udp_send_port: int = 51515,
        udp_recv_port: int = 51516,
        enable_udp_listener: bool = True,
    ) -> None:
        self.coord_dir = coord_dir
        self.udp_host = udp_host
        self.udp_send_port = udp_send_port
        self.udp_recv_port = udp_recv_port
        self.enable_udp_listener = enable_udp_listener

        self.coord_dir.mkdir(parents=True, exist_ok=True)
        self._status_path = self.coord_dir / "status.json"
        self._command_path = self.coord_dir / "command.txt"
        self._lock = threading.Lock()
        self._status = BridgeStatus()
        self._signal_callbacks: list[Callable[[MmmSignal, str], None]] = []
        self._listener_thread: threading.Thread | None = None
        self._listener_stop = threading.Event()

        if not self._status_path.exists():
            self._write_status(self._status)

    @property
    def status(self) -> BridgeStatus:
        with self._lock:
            return BridgeStatus(**self._status.to_dict())

    def on_signal(self, callback: Callable[[MmmSignal, str], None]) -> None:
        self._signal_callbacks.append(callback)

    def start_listener(self) -> None:
        if not self.enable_udp_listener or self._listener_thread:
            return
        self._listener_stop.clear()
        self._listener_thread = threading.Thread(
            target=self._udp_listen_loop, name="mmm-udp-listener", daemon=True
        )
        self._listener_thread.start()
        logger.info(
            "MMM UDP listener on %s:%s", self.udp_host, self.udp_recv_port
        )

    def stop_listener(self) -> None:
        self._listener_stop.set()
        if self._listener_thread:
            self._listener_thread.join(timeout=2)
            self._listener_thread = None

    def send_command(self, command: MmmCommand, detail: str = "") -> None:
        payload = command.value if not detail else f"{command.value}:{detail}"
        with self._lock:
            self._status.last_command = payload
            self._status.last_command_at = _utc_now()
            self._write_status_unlocked()

        self._command_path.write_text(payload, encoding="utf-8")
        self._send_udp(payload)
        logger.info("MMM command sent: %s", payload)

    def report_signal(self, signal: MmmSignal | str, detail: str = "") -> None:
        try:
            parsed = MmmSignal(str(signal).upper())
        except ValueError:
            parsed = MmmSignal.ERROR
            detail = detail or str(signal)

        with self._lock:
            self._status.mmm_signal = parsed.value
            self._status.last_signal_at = _utc_now()
            if detail:
                self._status.message = detail
            self._write_status_unlocked()

        for callback in self._signal_callbacks:
            try:
                callback(parsed, detail)
            except Exception:
                logger.exception("MMM signal callback failed")

        logger.info("MMM signal received: %s %s", parsed.value, detail)

    def set_phase(self, phase: str, message: str = "", **extra: object) -> None:
        with self._lock:
            self._status.phase = phase
            if message:
                self._status.message = message
            if extra:
                self._status.extra.update(extra)
            self._write_status_unlocked()

    def wait_for_signal(
        self,
        *signals: MmmSignal,
        timeout_secs: float,
        poll_interval: float = 0.25,
    ) -> MmmSignal | None:
        deadline = time.monotonic() + timeout_secs
        wanted = {s.value for s in signals}
        while time.monotonic() < deadline:
            current = self.status.mmm_signal
            if current in wanted:
                return MmmSignal(current)
            time.sleep(poll_interval)
        return None

    def clear_signal(self) -> None:
        with self._lock:
            self._status.mmm_signal = None
            self._write_status_unlocked()

    def _write_status(self, status: BridgeStatus) -> None:
        with self._lock:
            self._status = status
            self._write_status_unlocked()

    def _write_status_unlocked(self) -> None:
        self._status_path.write_text(
            json.dumps(self._status.to_dict(), indent=2), encoding="utf-8"
        )

    def _send_udp(self, payload: str) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(payload.encode("utf-8"), (self.udp_host, self.udp_send_port))
        except OSError as exc:
            logger.warning("UDP send failed (%s:%s): %s", self.udp_host, self.udp_send_port, exc)

    def _udp_listen_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.udp_host, self.udp_recv_port))
            sock.settimeout(1.0)
            while not self._listener_stop.is_set():
                try:
                    data, _addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    if self._listener_stop.is_set():
                        break
                    continue
                text = data.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                signal_name, _, detail = text.partition(":")
                self.report_signal(signal_name, detail)
        finally:
            sock.close()
