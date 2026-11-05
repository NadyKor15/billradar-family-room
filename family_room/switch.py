"""Thin clients for Switch 0.31.0: the agent protocol (one SSE stream down, HTTP up), the Gateway
(cookie session, admin setup) and the bundled Mattermost (seeding demo people only).

Stdlib only, so the coordinator process can run with nothing but Python and its own API key.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

log = logging.getLogger("family_room.switch")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class HttpError(RuntimeError):
    def __init__(self, status: int, body: Any, what: str):
        super().__init__(f"{what}: HTTP {status} {str(body)[:300]}")
        self.status, self.body = status, body


class Http:
    def __init__(self, base: str, headers: dict | None = None, cookies: bool = False):
        self.base = base.rstrip("/")
        self.headers = dict(headers or {})
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())] if cookies else []
        self.opener = urllib.request.build_opener(*handlers)

    def raw(self, method: str, path: str, body: Any = None, headers: dict | None = None,
            timeout: float = 30) -> tuple[int, bytes, dict]:
        h = dict(self.headers)
        h.update(headers or {})
        data = None
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        elif body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def call(self, method: str, path: str, body: Any = None, ok: tuple = (200, 201), **kw) -> Any:
        status, payload, _ = self.raw(method, path, body, **kw)
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = payload.decode(errors="replace")
        if status not in ok:
            raise HttpError(status, parsed, f"{method} {path}")
        return parsed


# ── agent side ───────────────────────────────────────────────────────────────

class Agent:
    """One Switch agent: its own API key, its own connection id, nothing else."""

    def __init__(self, api: str, agent_id: str, api_key: str, connection_id: str, name: str,
                 streaming: bool = True):
        self.api, self.id, self.name = api, agent_id, name
        self.conn = connection_id
        headers = {"Authorization": f"Bearer {api_key}"}
        if streaming:  # binds operations to our open stream; a caller with no stream must not send it (409)
            headers["X-Switch-Connection-Id"] = connection_id
        self.http = Http(api, headers=headers)
        self._key = api_key
        self.cursor = 0
        self._reopen = threading.Event()

    # operations and plain calls
    def op(self, name: str, **args) -> Any:
        return self.http.call("POST", f"/agents/{self.id}/ops/{name}", args)["result"]

    def op_raw(self, name: str, **args) -> tuple[int, bytes]:
        status, body, _ = self.http.raw("POST", f"/agents/{self.id}/ops/{name}", args)
        return status, body

    def post(self, room_id: str, content: str) -> str:
        return self.http.call("POST", f"/agents/{self.id}/message",
                              {"room_id": room_id, "content": content})["event_id"]

    def send_to(self, room_id: str, names: list[str], body: str) -> dict:
        """Addressed message: claim the room for this connection, then send_targeted_message."""
        self.op("connect_to_room", room_id=room_id)
        return self.op("send_targeted_message", body=body, target_names=names)

    def media(self, room_id: str, mxc: str) -> bytes:
        q = urllib.parse.urlencode({"mxc": mxc})
        status, body, _ = self.http.raw("GET", f"/agents/{self.id}/rooms/{room_id}/media?{q}", timeout=60)
        if status != 200:
            raise HttpError(status, body[:200], "media")
        return body

    def media_status(self, room_id: str, mxc: str) -> int:
        q = urllib.parse.urlencode({"mxc": mxc})
        return self.http.raw("GET", f"/agents/{self.id}/rooms/{room_id}/media?{q}", timeout=60)[0]

    def upload(self, room_id: str, filename: str, data: bytes, mimetype: str) -> Any:
        boundary = "----fr" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {mimetype}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        return self.http.call("POST", f"/agents/{self.id}/rooms/{room_id}/media", body,
                              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    # approval cards
    def open_card(self, session_id: str, request_id: str, room_id: str, title: str, detail: str,
                  options: list[tuple[str, str, str]], hours: float = 20) -> dict:
        expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        return self.http.call("POST", f"/agent-sessions/{session_id}/approvals", {
            "request_id": request_id, "turn_id": "t-" + request_id, "kind": "approval",
            "title": title, "detail": detail,
            "options": [{"id": i, "label": label, "decision": d} for i, label, d in options],
            "questions": [], "room_id": room_id, "thread_id": None, "expires_at": expires})

    def outcomes(self) -> list[dict]:
        return self.http.call("GET", "/agent-sessions/approvals/outcomes") or []

    def mark_delivered(self, session_id: str, request_id: str) -> None:
        self.http.call("POST", f"/agent-sessions/{session_id}/approvals/{request_id}/delivered")

    # stream
    def run_stream(self, rooms: list[str], on_event: Callable[[str, dict], None]) -> None:
        """Forever: open the SSE stream on our rooms, beat every 2 s, hand every domain event over."""
        threading.Thread(target=self._beat, daemon=True, name=f"{self.name}-beat").start()
        while True:
            q = urllib.parse.urlencode({"connection_id": self.conn, "scope": "all", "filter": "all",
                                        "start_from": "head", "rooms": ",".join(rooms),
                                        "client": "billradar-family-room", "client_version": "0.1.0"})
            req = urllib.request.Request(f"{self.api}/agents/{self.id}/events?{q}",
                                         headers={"Authorization": f"Bearer {self._key}",
                                                  "Accept": "text/event-stream"})
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    log.info("%s stream open", self.name)
                    ev, data = None, []
                    while not self._reopen.is_set():
                        line = r.readline()
                        if not line:
                            break
                        line = line.decode().rstrip("\n")
                        if line.startswith(":"):
                            continue
                        if line.startswith("event:"):
                            ev = line[6:].strip()
                        elif line.startswith("data:"):
                            data.append(line[5:].strip())
                        elif line == "" and data:
                            self._frame(ev, "\n".join(data), on_event)
                            ev, data = None, []
                    self._reopen.clear()
            except Exception as exc:  # noqa: BLE001 - logged, the loop reconnects with the same id
                log.warning("%s stream error: %r", self.name, exc)
            time.sleep(1)

    def _frame(self, ev: str | None, raw: str, on_event) -> None:
        try:
            d = json.loads(raw)
        except ValueError:
            return
        if isinstance(d.get("sequence"), int):
            self.cursor = max(self.cursor, d["sequence"])
        if ev in ("message", "command", "room_join"):
            try:
                on_event(ev, d)
            except Exception:  # noqa: BLE001 - one bad event must not stop the agent
                log.exception("%s handler failed on %s", self.name, ev)

    def _beat(self) -> None:
        while True:
            status, body, _ = self.http.raw("POST", f"/agents/{self.id}/connection/beat",
                                            {"connection_id": self.conn, "cursor": self.cursor}, timeout=10)
            if status in (404, 409):
                self._reopen.set()
            elif status != 200:
                log.warning("%s beat %s %s", self.name, status, body[:120])
            time.sleep(2)


# ── operator side (seeding, verify) ─────────────────────────────────────────

def gateway(api: str, email: str, password: str) -> Http:
    gw = Http(api, cookies=True)
    gw.call("POST", "/gateway/auth/login", {"email": email, "password": password})
    return gw


def mattermost(base: str, login_id: str, password: str) -> Http:
    status, body, headers = Http(base).raw("POST", "/api/v4/users/login",
                                           {"login_id": login_id, "password": password})
    if status != 200:
        raise HttpError(status, body[:200], f"mattermost login {login_id}")
    token = headers.get("Token") or headers.get("token")
    return Http(base, headers={"Authorization": f"Bearer {token}"})


def mm_post(mm: Http, channel_id: str, message: str, files: list[tuple[str, bytes, str]] | None = None) -> dict:
    ids = []
    for name, data, mime in files or []:
        boundary = "----fr" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"channel_id\"\r\n\r\n{channel_id}\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        res = mm.call("POST", "/api/v4/files", body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, ok=(201,))
        ids.append(res["file_infos"][0]["id"])
    return mm.call("POST", "/api/v4/posts", {"channel_id": channel_id, "message": message, "file_ids": ids},
                   ok=(201,))
