"""ASGI request-size guard for the upload route (M11D Core).

FastAPI parses a multipart body before the handler runs, so the size limit
must be enforced at the ASGI layer: a declared `Content-Length` over the
ceiling is refused up front, and a streamed/chunked body is counted and
refused the moment it exceeds it. The refusal is a controlled
`OperationsRequestRejectedError` (413), handled by the normal error handlers.
"""

from __future__ import annotations

from typing import Any

from ap_agent.exceptions import OperationsRequestRejectedError

__all__ = ["UPLOAD_PATH", "RequestSizeLimitMiddleware"]

UPLOAD_PATH = "/api/v1/operations/submissions"


class RequestSizeLimitMiddleware:
    def __init__(self, app: Any, *, maximum_request_bytes: int) -> None:
        self._app = app
        self._maximum = maximum_request_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != UPLOAD_PATH:
            await self._app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = self._maximum + 1

                if declared > self._maximum:
                    await self._reject(scope, receive, send)
                    return

        received = 0

        async def counting_receive() -> dict:
            nonlocal received
            message = await receive()

            if message["type"] == "http.request":
                received += len(message.get("body", b""))

                if received > self._maximum:
                    raise OperationsRequestRejectedError("REQUEST_TOO_LARGE", http_status=413)

            return message

        await self._app(scope, counting_receive, send)

    async def _reject(self, scope: dict, receive: Any, send: Any) -> None:
        import json
        from datetime import datetime, timezone
        from uuid import uuid4

        body = json.dumps(
            {
                "request_id": str(uuid4()),
                "errors": ["REQUEST_TOO_LARGE"],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
