"""Signal names exchanged between Python and Mini Mouse Macro."""

from __future__ import annotations

from enum import Enum


class MmmCommand(str, Enum):
    """Commands Python sends to MMM (UDP or command file)."""

    START_LOOP1 = "START_LOOP1"
    REQUEST_SAFE = "REQUEST_SAFE"
    RUN_LOOP2 = "RUN_LOOP2"
    RESUME_LOOP1 = "RESUME_LOOP1"
    STOP = "STOP"


class MmmSignal(str, Enum):
    """Signals MMM sends back to Python (UDP or status file)."""

    BUSY = "BUSY"
    SAFE = "SAFE"
    LOOP2_DONE = "LOOP2_DONE"
    LOOP1_STOPPED = "LOOP1_STOPPED"
    ERROR = "ERROR"


class CoordinatorPhase(str, Enum):
    IDLE = "idle"
    LOOP1_RUNNING = "loop1_running"
    WAITING_SAFE = "waiting_safe"
    LOOP1_SAFE = "loop1_safe"
    LOOP2_RUNNING = "loop2_running"
