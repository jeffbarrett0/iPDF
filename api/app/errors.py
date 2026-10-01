from __future__ import annotations


class AppError(Exception):
    """A user-facing, recoverable error. `hint` tells the user what to try next."""

    def __init__(self, code: str, message: str, status: int = 400, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "hint": self.hint}}


class ToolUnavailable(AppError):
    def __init__(self, tool: str, hint: str):
        super().__init__(
            "tool_unavailable", f"{tool} is not available on this machine.", 503, hint
        )
