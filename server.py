#!/usr/bin/env python3
"""Local persistent group-chat backend for multiple Codex conversations."""

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import sqlite3
import threading
import time
import traceback
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MAX_BODY_BYTES = 70 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_EVENTS_PER_POLL = 200
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
EVENT_CONDITION = threading.Condition()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def token():
    return secrets.token_urlsafe(32)


def token_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def compact_id(prefix):
    return "{}-{}".format(prefix, secrets.token_hex(5))


def safe_filename(value):
    name = os.path.basename(value or "attachment")
    name = re.sub(r"[^A-Za-z0-9._()\- \u0080-\uffff]", "_", name).strip(" .")
    return (name or "attachment")[:180]


class ChatStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.db_path = self.data_dir / "chat.sqlite3"
        self.files_dir = self.data_dir / "files"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self):
        last_error = None
        for attempt in range(6):
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(self.db_path), timeout=10)
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "unable to open database file" not in str(exc).lower() or attempt == 5:
                    raise
                time.sleep(0.05 * (2 ** attempt))
        else:
            raise last_error
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def connection(self):
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self):
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS rooms (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    invite_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin', 'collaborator')),
                    token_hash TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1,
                    joined_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_admin_per_room
                    ON members(room_id) WHERE role = 'admin' AND active = 1;
                CREATE INDEX IF NOT EXISTS members_room_idx ON members(room_id, active);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    sender_id TEXT REFERENCES members(id),
                    recipient_id TEXT REFERENCES members(id),
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_room_id_idx ON events(room_id, id);
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL REFERENCES rooms(id),
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def bootstrap(self, room_id, display_name, admin_name):
        if not IDENTIFIER_RE.match(room_id):
            raise ValueError("room_id 只能包含字母、数字、点、下划线和连字符，长度不超过 64")
        invite = token()
        admin_token = token()
        admin_id = compact_id("admin")
        created = utc_now()
        with self.connection() as conn:
            if conn.execute("SELECT 1 FROM rooms WHERE id = ?", (room_id,)).fetchone():
                raise Conflict("聊天室已存在")
            conn.execute(
                "INSERT INTO rooms(id, display_name, invite_hash, created_at) VALUES (?, ?, ?, ?)",
                (room_id, display_name, token_hash(invite), created),
            )
            conn.execute(
                """
                INSERT INTO members(id, room_id, name, role, token_hash, joined_at, last_seen_at)
                VALUES (?, ?, ?, 'admin', ?, ?, ?)
                """,
                (admin_id, room_id, admin_name, token_hash(admin_token), created, created),
            )
            event_id = self._insert_event(
                conn,
                room_id,
                None,
                None,
                "system",
                "聊天室已创建，管理员为 {}".format(admin_name),
                {"action": "room_created", "admin_member_id": admin_id},
            )
        self.notify()
        return {
            "room_id": room_id,
            "display_name": display_name,
            "admin": {
                "member_id": admin_id,
                "name": admin_name,
                "role": "admin",
                "token": admin_token,
            },
            "invite_token": invite,
            "cursor": event_id,
        }

    def join(self, room_id, name, invite):
        if not name or len(name) > 100:
            raise ValueError("成员名称不能为空且最多 100 个字符")
        member_token = token()
        member_id = compact_id("collab")
        joined = utc_now()
        with self.connection() as conn:
            room = conn.execute(
                "SELECT * FROM rooms WHERE id = ? AND active = 1", (room_id,)
            ).fetchone()
            if not room or not secrets.compare_digest(room["invite_hash"], token_hash(invite)):
                raise Unauthorized("邀请令牌无效")
            conn.execute(
                """
                INSERT INTO members(id, room_id, name, role, token_hash, joined_at, last_seen_at)
                VALUES (?, ?, ?, 'collaborator', ?, ?, ?)
                """,
                (member_id, room_id, name, token_hash(member_token), joined, joined),
            )
            event_id = self._insert_event(
                conn,
                room_id,
                None,
                None,
                "system",
                "{} 已加入聊天室".format(name),
                {"action": "member_joined", "member_id": member_id, "name": name},
            )
        self.notify()
        return {
            "room_id": room_id,
            "member_id": member_id,
            "name": name,
            "role": "collaborator",
            "token": member_token,
            "cursor": event_id,
        }

    def authenticate(self, room_id, bearer):
        if not bearer:
            raise Unauthorized("缺少 Bearer 令牌")
        hashed = token_hash(bearer)
        with self.connection() as conn:
            member = conn.execute(
                """
                SELECT * FROM members
                WHERE room_id = ? AND token_hash = ? AND active = 1
                """,
                (room_id, hashed),
            ).fetchone()
            if not member:
                raise Unauthorized("成员令牌无效或已停用")
            conn.execute(
                "UPDATE members SET last_seen_at = ? WHERE id = ?",
                (utc_now(), member["id"]),
            )
            return dict(member)

    def roster(self, room_id):
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT id AS member_id, name, role, active, joined_at, last_seen_at
                FROM members WHERE room_id = ? ORDER BY role, joined_at
                """,
                (room_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def resolve_recipient(self, conn, room_id, value):
        if not value:
            return None
        row = conn.execute(
            "SELECT id FROM members WHERE room_id = ? AND id = ? AND active = 1",
            (room_id, value),
        ).fetchone()
        if row:
            return row["id"]
        rows = conn.execute(
            "SELECT id FROM members WHERE room_id = ? AND name = ? AND active = 1",
            (room_id, value),
        ).fetchall()
        if not rows:
            raise NotFound("找不到收件人：{}".format(value))
        if len(rows) > 1:
            raise Conflict("成员名称重复，请使用 member_id")
        return rows[0]["id"]

    def create_message(self, member, payload):
        kind = payload.get("kind", "text")
        text = payload.get("text") or ""
        recipient = payload.get("to")
        if kind not in ("text", "file", "image", "path"):
            raise ValueError("kind 必须是 text、file、image 或 path")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError("消息文本过长")

        decoded = None
        filename = None
        mime_type = None
        digest = None
        path_value = None
        metadata = {}

        if kind in ("file", "image"):
            encoded = payload.get("content_base64")
            filename = safe_filename(payload.get("filename"))
            if not encoded:
                raise ValueError("文件消息缺少 content_base64")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except Exception:
                raise ValueError("content_base64 不是有效的 Base64")
            if len(decoded) > MAX_FILE_BYTES:
                raise PayloadTooLarge("单个文件不能超过 50 MiB")
            mime_type = payload.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
            digest = hashlib.sha256(decoded).hexdigest()
        elif kind == "path":
            raw_path = payload.get("path")
            if not raw_path:
                raise ValueError("路径消息缺少 path")
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                raise ValueError("同机共享必须使用绝对路径")
            try:
                path = path.resolve(strict=True)
            except FileNotFoundError:
                raise NotFound("共享路径不存在")
            if not path.is_file():
                raise ValueError("当前只允许共享文件路径")
            stat = path.stat()
            path_value = str(path)
            metadata = {
                "path": path_value,
                "filename": path.name,
                "size": stat.st_size,
                "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "local_only": True,
            }
        elif not text:
            raise ValueError("文本消息不能为空")

        file_id = None
        storage_path = None
        with self.connection() as conn:
            recipient_id = self.resolve_recipient(conn, member["room_id"], recipient)
            if decoded is not None:
                file_id = compact_id("file")
                room_dir = self.files_dir / member["room_id"]
                room_dir.mkdir(parents=True, exist_ok=True)
                storage_path = room_dir / "{}-{}".format(file_id, filename)
                metadata = {
                    "file_id": file_id,
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": len(decoded),
                    "sha256": digest,
                    "local_path": str(storage_path),
                    "download_url": "/v1/rooms/{}/files/{}".format(
                        urllib.parse.quote(member["room_id"]), file_id
                    ),
                }
            event_id = self._insert_event(
                conn,
                member["room_id"],
                member["id"],
                recipient_id,
                kind,
                text,
                metadata,
            )
            if decoded is not None:
                temp_path = storage_path.with_suffix(storage_path.suffix + ".tmp")
                try:
                    with open(temp_path, "wb") as handle:
                        handle.write(decoded)
                    os.replace(str(temp_path), str(storage_path))
                    conn.execute(
                        """
                        INSERT INTO files(
                            id, room_id, event_id, filename, mime_type, size,
                            sha256, storage_path, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            file_id,
                            member["room_id"],
                            event_id,
                            filename,
                            mime_type,
                            len(decoded),
                            digest,
                            str(storage_path),
                            utc_now(),
                        ),
                    )
                except Exception:
                    if temp_path.exists():
                        temp_path.unlink()
                    if storage_path.exists():
                        storage_path.unlink()
                    raise
        self.notify()
        return self.get_event(member["room_id"], event_id)

    def _insert_event(self, conn, room_id, sender_id, recipient_id, kind, text, metadata):
        cursor = conn.execute(
            """
            INSERT INTO events(room_id, sender_id, recipient_id, kind, text, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_id,
                sender_id,
                recipient_id,
                kind,
                text,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                utc_now(),
            ),
        )
        return cursor.lastrowid

    def get_event(self, room_id, event_id):
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT e.*, s.name AS sender_name, s.role AS sender_role,
                       r.name AS recipient_name
                FROM events e
                LEFT JOIN members s ON s.id = e.sender_id
                LEFT JOIN members r ON r.id = e.recipient_id
                WHERE e.room_id = ? AND e.id = ?
                """,
                (room_id, event_id),
            ).fetchone()
            if not row:
                raise NotFound("消息不存在")
            return self._event_dict(row)

    def _event_dict(self, row):
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "kind": row["kind"],
            "text": row["text"],
            "sender": (
                {
                    "member_id": row["sender_id"],
                    "name": row["sender_name"],
                    "role": row["sender_role"],
                }
                if row["sender_id"]
                else None
            ),
            "recipient": (
                {"member_id": row["recipient_id"], "name": row["recipient_name"]}
                if row["recipient_id"]
                else None
            ),
            "metadata": json.loads(row["metadata_json"]),
        }

    def poll(self, member, after, wait_seconds):
        deadline = time.monotonic() + wait_seconds
        cursor = after
        while True:
            with self.connection() as conn:
                max_row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS cursor FROM events WHERE room_id = ?",
                    (member["room_id"],),
                ).fetchone()
                current_cursor = max(cursor, max_row["cursor"])
                rows = conn.execute(
                    """
                    SELECT e.*, s.name AS sender_name, s.role AS sender_role,
                           r.name AS recipient_name
                    FROM events e
                    LEFT JOIN members s ON s.id = e.sender_id
                    LEFT JOIN members r ON r.id = e.recipient_id
                    WHERE e.room_id = ? AND e.id > ?
                      AND (e.sender_id IS NULL OR e.sender_id <> ?)
                      AND (e.recipient_id IS NULL OR e.recipient_id = ?)
                    ORDER BY e.id ASC LIMIT ?
                    """,
                    (
                        member["room_id"],
                        cursor,
                        member["id"],
                        member["id"],
                        MAX_EVENTS_PER_POLL,
                    ),
                ).fetchall()
                if rows:
                    events = [self._event_dict(row) for row in rows]
                    return {"events": events, "cursor": rows[-1]["id"]}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"events": [], "cursor": current_cursor}
            cursor = current_cursor
            with EVENT_CONDITION:
                EVENT_CONDITION.wait(timeout=min(remaining, 1.0))

    def timeline(self, member, after, wait_seconds):
        if member["role"] != "admin":
            raise Forbidden("只有管理员可以查看完整时间线")
        deadline = time.monotonic() + wait_seconds
        cursor = after
        while True:
            with self.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT e.*, s.name AS sender_name, s.role AS sender_role,
                           r.name AS recipient_name
                    FROM events e
                    LEFT JOIN members s ON s.id = e.sender_id
                    LEFT JOIN members r ON r.id = e.recipient_id
                    WHERE e.room_id = ? AND e.id > ?
                    ORDER BY e.id ASC LIMIT ?
                    """,
                    (member["room_id"], cursor, MAX_EVENTS_PER_POLL),
                ).fetchall()
                if rows:
                    return {
                        "events": [self._event_dict(row) for row in rows],
                        "cursor": rows[-1]["id"],
                    }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"events": [], "cursor": cursor}
            with EVENT_CONDITION:
                EVENT_CONDITION.wait(timeout=min(remaining, 1.0))

    def history(self, member, limit):
        with self.connection() as conn:
            if member["role"] == "admin":
                visibility = "1 = 1"
                args = [member["room_id"]]
            else:
                visibility = "(e.recipient_id IS NULL OR e.recipient_id = ? OR e.sender_id = ?)"
                args = [member["room_id"], member["id"], member["id"]]
            args.append(limit)
            rows = conn.execute(
                """
                SELECT * FROM (
                    SELECT e.*, s.name AS sender_name, s.role AS sender_role,
                           r.name AS recipient_name
                    FROM events e
                    LEFT JOIN members s ON s.id = e.sender_id
                    LEFT JOIN members r ON r.id = e.recipient_id
                    WHERE e.room_id = ? AND {}
                    ORDER BY e.id DESC LIMIT ?
                ) ORDER BY id ASC
                """.format(visibility),
                args,
            ).fetchall()
            return [self._event_dict(row) for row in rows]

    def deactivate(self, admin, member_id):
        if admin["role"] != "admin":
            raise Forbidden("只有管理员可以停用成员")
        if member_id == admin["id"]:
            raise ValueError("不能停用当前管理员")
        with self.connection() as conn:
            target = conn.execute(
                "SELECT * FROM members WHERE room_id = ? AND id = ? AND active = 1",
                (admin["room_id"], member_id),
            ).fetchone()
            if not target:
                raise NotFound("成员不存在或已停用")
            conn.execute("UPDATE members SET active = 0 WHERE id = ?", (member_id,))
            event_id = self._insert_event(
                conn,
                admin["room_id"],
                None,
                None,
                "system",
                "{} 已被管理员停用".format(target["name"]),
                {"action": "member_deactivated", "member_id": member_id},
            )
        self.notify()
        return {"deactivated": member_id, "event_id": event_id}

    def rotate_invite(self, admin):
        if admin["role"] != "admin":
            raise Forbidden("只有管理员可以轮换邀请令牌")
        invite = token()
        with self.connection() as conn:
            conn.execute(
                "UPDATE rooms SET invite_hash = ? WHERE id = ?",
                (token_hash(invite), admin["room_id"]),
            )
        return {"room_id": admin["room_id"], "invite_token": invite}

    def file_info(self, room_id, file_id):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE room_id = ? AND id = ?",
                (room_id, file_id),
            ).fetchone()
            if not row:
                raise NotFound("文件不存在")
            return dict(row)

    @staticmethod
    def notify():
        with EVENT_CONDITION:
            EVENT_CONDITION.notify_all()


class ApiError(Exception):
    status = HTTPStatus.BAD_REQUEST


class Unauthorized(ApiError):
    status = HTTPStatus.UNAUTHORIZED


class Forbidden(ApiError):
    status = HTTPStatus.FORBIDDEN


class NotFound(ApiError):
    status = HTTPStatus.NOT_FOUND


class Conflict(ApiError):
    status = HTTPStatus.CONFLICT


class PayloadTooLarge(ApiError):
    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "CodexGroupChat/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(
            "{} {} {}".format(
                utc_now(),
                self.address_string(),
                fmt % args,
            ),
            flush=True,
        )

    @property
    def store(self):
        return self.server.store

    def _json_body(self):
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("缺少 Content-Length")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("Content-Length 无效")
        if length < 0 or length > MAX_BODY_BYTES:
            raise PayloadTooLarge("请求体过大")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体必须是 UTF-8 JSON")
        if not isinstance(payload, dict):
            raise ValueError("JSON 请求体必须是对象")
        return payload

    def _bearer(self):
        value = self.headers.get("Authorization", "")
        if value.startswith("Bearer "):
            return value[7:]
        return None

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path, content_type):
        if not path.is_file():
            raise NotFound("页面资源不存在")
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob: data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc):
        if isinstance(exc, ApiError):
            status = exc.status
        elif isinstance(exc, ValueError):
            status = HTTPStatus.BAD_REQUEST
        else:
            traceback.print_exc()
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._send_json(
            status,
            {"error": exc.__class__.__name__, "message": str(exc) or "服务器错误"},
        )

    def _route(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(item) for item in parsed.path.split("/") if item]
        query = urllib.parse.parse_qs(parsed.query)
        return parts, query

    def do_GET(self):
        try:
            parts, query = self._route()
            raw_path = self.path.split("?", 1)[0]
            if not parts:
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/dashboard/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if raw_path == "/dashboard":
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", "/dashboard/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if raw_path == "/dashboard/":
                self._send_static(
                    self.server.web_dir / "index.html",
                    "text/html; charset=utf-8",
                )
                return
            if parts == ["dashboard", "styles.css"]:
                self._send_static(self.server.web_dir / "styles.css", "text/css; charset=utf-8")
                return
            if parts == ["dashboard", "app.js"]:
                self._send_static(
                    self.server.web_dir / "app.js",
                    "text/javascript; charset=utf-8",
                )
                return
            if parts == ["health"]:
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "codex-group-chat",
                        "version": "1.1.1",
                        "time": utc_now(),
                    },
                )
                return
            if len(parts) >= 3 and parts[:2] == ["v1", "rooms"]:
                room_id = parts[2]
                member = self.store.authenticate(room_id, self._bearer())
                if len(parts) == 4 and parts[3] == "members":
                    self._send_json(HTTPStatus.OK, {"members": self.store.roster(room_id)})
                    return
                if len(parts) == 4 and parts[3] == "events":
                    after = int(query.get("after", ["0"])[0])
                    wait = float(query.get("wait", ["0"])[0])
                    if after < 0 or wait < 0 or wait > 55:
                        raise ValueError("after 必须非负，wait 必须在 0 到 55 秒之间")
                    self._send_json(HTTPStatus.OK, self.store.poll(member, after, wait))
                    return
                if len(parts) == 4 and parts[3] == "timeline":
                    after = int(query.get("after", ["0"])[0])
                    wait = float(query.get("wait", ["0"])[0])
                    if after < 0 or wait < 0 or wait > 55:
                        raise ValueError("after 必须非负，wait 必须在 0 到 55 秒之间")
                    self._send_json(
                        HTTPStatus.OK, self.store.timeline(member, after, wait)
                    )
                    return
                if len(parts) == 4 and parts[3] == "history":
                    limit = int(query.get("limit", ["50"])[0])
                    if limit < 1 or limit > 500:
                        raise ValueError("limit 必须在 1 到 500 之间")
                    self._send_json(
                        HTTPStatus.OK, {"events": self.store.history(member, limit)}
                    )
                    return
                if len(parts) == 5 and parts[3] == "files":
                    info = self.store.file_info(room_id, parts[4])
                    path = Path(info["storage_path"])
                    if not path.is_file():
                        raise NotFound("文件记录存在，但磁盘文件缺失")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", info["mime_type"])
                    self.send_header("Content-Length", str(info["size"]))
                    encoded = urllib.parse.quote(info["filename"])
                    self.send_header(
                        "Content-Disposition", "attachment; filename*=UTF-8''{}".format(encoded)
                    )
                    self.send_header("X-Content-SHA256", info["sha256"])
                    self.send_header("Cache-Control", "private, no-store")
                    self.end_headers()
                    with open(path, "rb") as handle:
                        shutil.copyfileobj(handle, self.wfile)
                    return
            raise NotFound("接口不存在")
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        try:
            parts, _query = self._route()
            if parts == ["v1", "rooms", "bootstrap"]:
                payload = self._json_body()
                room_id = payload.get("room_id") or "codex-team"
                result = self.store.bootstrap(
                    room_id,
                    payload.get("display_name") or room_id,
                    payload.get("admin_name") or "管理员",
                )
                self._send_json(HTTPStatus.CREATED, result)
                return
            if len(parts) >= 4 and parts[:2] == ["v1", "rooms"]:
                room_id = parts[2]
                if parts[3:] == ["members", "join"]:
                    payload = self._json_body()
                    result = self.store.join(
                        room_id, payload.get("name") or "", payload.get("invite_token") or ""
                    )
                    self._send_json(HTTPStatus.CREATED, result)
                    return
                member = self.store.authenticate(room_id, self._bearer())
                if parts[3:] == ["messages"]:
                    result = self.store.create_message(member, self._json_body())
                    self._send_json(HTTPStatus.CREATED, {"event": result})
                    return
                if len(parts) == 6 and parts[3] == "members" and parts[5] == "deactivate":
                    result = self.store.deactivate(member, parts[4])
                    self._send_json(HTTPStatus.OK, result)
                    return
                if parts[3:] == ["invite", "rotate"]:
                    result = self.store.rotate_invite(member)
                    self._send_json(HTTPStatus.OK, result)
                    return
            raise NotFound("接口不存在")
        except Exception as exc:
            self._error(exc)


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, store, web_dir):
        self.store = store
        self.web_dir = Path(web_dir).resolve()
        super().__init__(address, handler)


def parse_args():
    parser = argparse.ArgumentParser(description="Codex group chat local backend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get(
            "CODEX_GROUP_CHAT_DATA_DIR",
            "~/Library/Application Support/CodexGroupChat/data",
        ),
    )
    parser.add_argument(
        "--web-dir",
        default=str(Path(__file__).resolve().parent / "web"),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("安全限制：此服务只能绑定本机回环地址")
    store = ChatStore(args.data_dir)
    server = ChatServer((args.host, args.port), ChatHandler, store, args.web_dir)

    def stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(
        "{} codex-group-chat listening on http://{}:{} data={}".format(
            utc_now(), args.host, args.port, store.data_dir
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        print("{} codex-group-chat stopped".format(utc_now()), flush=True)


if __name__ == "__main__":
    main()
