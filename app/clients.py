"""Thin clients for talking between the three services."""

from __future__ import annotations

import os

from app.common import AppError, http_json


class IntakeClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("INTAKE_URL", "http://127.0.0.1:8301")).rstrip("/")

    def health(self) -> dict:
        return http_json(self.base_url + "/health")[1]

    def list_sources(self) -> list[dict]:
        return http_json(self.base_url + "/api/sources")[1]["sources"]

    def source_meta(self, source_id: int) -> dict:
        return http_json(self.base_url + f"/api/sources/{source_id}")[1]

    def head_seq(self, source_id: int) -> int:
        return int(self.source_meta(source_id)["source"]["head_seq"])

    def entries(self, source_id: int, after_seq: int = 0, limit: int = 2000) -> list[dict]:
        url = self.base_url + f"/api/sources/{source_id}/entries?after_seq={after_seq}&limit={limit}"
        return http_json(url)[1]["entries"]

    def all_entries(self, source_id: int, page_size: int = 2000) -> list[dict]:
        out: list[dict] = []
        cursor = 0
        while True:
            page = self.entries(source_id, after_seq=cursor, limit=page_size)
            if not page:
                break
            out.extend(page)
            cursor = int(page[-1]["seq"])
            if len(page) < page_size:
                break
        return out

    def create_revision(self, source_id: int, payload: dict) -> tuple[dict, bool]:
        status, body = http_json(
            self.base_url + f"/api/sources/{source_id}/revisions",
            method="POST",
            payload=payload,
        )
        return body["entry"], bool(body.get("created"))

    def revoke(self, source_id: int, payload: dict) -> tuple[dict, bool]:
        status, body = http_json(
            self.base_url + f"/api/sources/{source_id}/revocations",
            method="POST",
            payload=payload,
        )
        return body["entry"], bool(body.get("created"))


class MergerClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.environ.get("MERGER_URL", "http://127.0.0.1:8302")).rstrip("/")

    def _get(self, path: str) -> tuple[int, dict]:
        return http_json(self.base_url + path)

    def state(self, source_id: int, params: dict) -> dict:
        from urllib.parse import urlencode

        return self._get(f"/api/sources/{source_id}/state?{urlencode(params)}")[1]

    def preview(self, source_id: int, payload: dict) -> dict:
        return http_json(
            self.base_url + f"/api/sources/{source_id}/preview",
            method="POST",
            payload=payload,
        )[1]

    def draft(self, source_id: int) -> dict | None:
        try:
            return http_json(self.base_url + f"/api/sources/{source_id}/draft")[1]["draft"]
        except AppError as exc:
            if exc.status == 404:
                return None
            raise

    def save_draft(self, source_id: int, payload: dict) -> dict:
        return http_json(
            self.base_url + f"/api/sources/{source_id}/draft",
            method="PUT",
            payload=payload,
        )[1]["draft"]

    def discard_draft(self, source_id: int) -> dict:
        return http_json(
            self.base_url + f"/api/sources/{source_id}/draft",
            method="DELETE",
        )[1]

    def commit(self, source_id: int, payload: dict) -> dict:
        return http_json(
            self.base_url + f"/api/sources/{source_id}/commit",
            method="POST",
            payload=payload,
        )[1]
