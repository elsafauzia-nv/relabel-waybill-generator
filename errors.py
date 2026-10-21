"""Run failures, each with its own exit code so callers can tell them apart."""

from __future__ import annotations

from enum import Enum


class Failure(Enum):
    # value: (exit code, short label)
    INVALID_TRACKING_ID = (2, "invalid tracking ID")
    VENDOR_MISMATCH = (3, "vendor mismatch / no relabel label")
    OPERATOR_ERROR = (4, "Operator V2 error")
    TIMEOUT = (5, "timeout")
    MISSING_PRINT_EVENT = (6, "missing print event")
    WEBSOCKET_FAILURE = (7, "printer WebSocket failure")
    PRINTER_UNSUPPORTED = (8, "printer type not capturable")
    ZPL_EXTRACTION = (9, "ZPL extraction failed")
    LABELARY = (10, "Labelary rendering failed")
    AUTH = (11, "sign-in failed")
    BROWSER = (12, "browser automation failed")
    INPUT = (13, "invalid input")
    UNRESOLVED_PLACEHOLDERS = (14, "unresolved template placeholders")
    DUPLICATE = (15, "duplicate tracking ID")
    VERIFICATION = (16, "waybill verification failed")

    @property
    def exit_code(self) -> int:
        return self.value[0]

    @property
    def label(self) -> str:
        return self.value[1]


class RunError(Exception):
    def __init__(self, failure: Failure, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.failure = failure
        self.message = message
        self.hint = hint
