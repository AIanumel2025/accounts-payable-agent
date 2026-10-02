"""A minimal in-memory S3 client double with failure injection (test-only)."""

from __future__ import annotations

import io
from typing import Any, Optional

from botocore.exceptions import ClientError


def _client_error(status: int, code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}}, operation)


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_on: Optional[str] = None
        self.corrupt_get = False
        self.truncate_get = False
        self.lie_about_metadata = False

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, {k: v for k, v in kwargs.items() if k != "Body"}))

        if self.fail_on == name:
            raise ConnectionError("injected outage")

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._record("head_object", Bucket=Bucket, Key=Key)
        stored = self.objects.get((Bucket, Key))

        if stored is None:
            raise _client_error(404, "404", "HeadObject")

        metadata = dict(stored["Metadata"])

        if self.lie_about_metadata:
            metadata["sha256"] = "0" * 64

        return {"ContentLength": len(stored["Body"]), "ContentType": stored["ContentType"], "Metadata": metadata}

    def put_object(self, *, Bucket: str, Key: str, Body: Any, **kwargs: Any) -> dict[str, Any]:
        self._record("put_object", Bucket=Bucket, Key=Key, **kwargs)
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = {
            "Body": data,
            "ContentType": kwargs.get("ContentType"),
            "Metadata": kwargs.get("Metadata", {}),
            "Params": {k: v for k, v in kwargs.items()},
        }
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._record("get_object", Bucket=Bucket, Key=Key)
        stored = self.objects.get((Bucket, Key))

        if stored is None:
            raise _client_error(404, "NoSuchKey", "GetObject")

        data = stored["Body"]

        if self.corrupt_get:
            data = b"X" + data[1:]

        if self.truncate_get:
            data = data[:-1]

        return {"Body": io.BytesIO(data)}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._record("delete_object", Bucket=Bucket, Key=Key)
        self.objects.pop((Bucket, Key), None)
        return {}

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)
