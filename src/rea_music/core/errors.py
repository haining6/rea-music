"""统一错误体系。MCP / CLI / API 共用同一组错误码，结构化返回给 Agent。"""

from __future__ import annotations


class REAError(Exception):
    """REA-Music 基础错误。code 为机器可读错误码，hint 给 Agent 的恢复指引。"""

    code = "rea_error"

    def __init__(self, message: str, *, code: str | None = None,
                 hint: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"ok": False, "error": {"code": self.code, "message": self.message}}
        if self.hint:
            payload["error"]["hint"] = self.hint
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class FileNotFound(REAError):
    code = "file_not_found"


class InvalidAudio(REAError):
    code = "invalid_audio"


class JobNotFound(REAError):
    code = "job_not_found"


class AnalysisNotFound(REAError):
    code = "analysis_not_found"


class ValidationError(REAError):
    code = "validation_error"


class CapabilityUnavailable(REAError):
    code = "capability_unavailable"


class BackendUnavailable(REAError):
    code = "backend_unavailable"


class GenerationFailed(REAError):
    code = "generation_failed"
