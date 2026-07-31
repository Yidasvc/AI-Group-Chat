#!/usr/bin/env python3
"""Command-line client for the local Codex group-chat service."""

import argparse
import base64
import fcntl
import json
import mimetypes
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path


DEFAULT_SERVER = "http://127.0.0.1:8765"
CONFIG_DIR = Path(os.environ.get("CODEX_GROUP_CHAT_CONFIG_DIR", "~/.codex-group-chat")).expanduser()
CONFIG_PATH = CONFIG_DIR / "identities.json"
LOCK_PATH = CONFIG_DIR / "identities.lock"


class ClientError(Exception):
    pass


@contextmanager
def config_lock():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(str(CONFIG_DIR), 0o700)
    with open(LOCK_PATH, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_config_unlocked():
    if not CONFIG_PATH.exists():
        return {"identities": {}}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ClientError("无法读取配置 {}: {}".format(CONFIG_PATH, exc))
    if not isinstance(data, dict) or not isinstance(data.get("identities"), dict):
        raise ClientError("配置文件格式无效：{}".format(CONFIG_PATH))
    return data


def save_config_unlocked(data):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="identities.", suffix=".tmp", dir=str(CONFIG_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, CONFIG_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_identity(alias, identity):
    with config_lock():
        data = load_config_unlocked()
        data["identities"][alias] = identity
        save_config_unlocked(data)


def update_cursor(alias, cursor):
    with config_lock():
        data = load_config_unlocked()
        identity = data["identities"].get(alias)
        if not identity:
            raise ClientError("找不到本地身份别名：{}".format(alias))
        identity["cursor"] = max(int(identity.get("cursor", 0)), int(cursor))
        save_config_unlocked(data)


def get_identity(alias):
    with config_lock():
        identity = load_config_unlocked()["identities"].get(alias)
    if not identity:
        raise ClientError(
            "找不到本地身份别名 {!r}；请先执行 bootstrap 或 join".format(alias)
        )
    return identity


def request(server, method, path, payload=None, bearer=None, raw=False, timeout=65):
    url = server.rstrip("/") + path
    headers = {"Accept": "application/json", "User-Agent": "cgchat/1.1"}
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
        data = response.read()
        if raw:
            return data, dict(response.headers)
        return json.loads(data.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body_text).get("message", body_text)
        except ValueError:
            detail = body_text
        raise ClientError("服务返回 HTTP {}：{}".format(exc.code, detail))
    except urllib.error.URLError as exc:
        raise ClientError("无法连接聊天室服务 {}：{}".format(server, exc.reason))


def room_path(identity, suffix):
    room_id = urllib.parse.quote(identity["room_id"], safe="")
    return "/v1/rooms/{}{}".format(room_id, suffix)


def auth_request(identity, method, suffix, payload=None, raw=False, timeout=65):
    return request(
        identity["server"],
        method,
        room_path(identity, suffix),
        payload=payload,
        bearer=identity["token"],
        raw=raw,
        timeout=timeout,
    )


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_bootstrap(args):
    payload = {
        "room_id": args.room,
        "display_name": args.display_name or args.room,
        "admin_name": args.admin_name,
    }
    result = request(args.server, "POST", "/v1/rooms/bootstrap", payload)
    admin = result["admin"]
    identity = {
        "server": args.server,
        "room_id": result["room_id"],
        "member_id": admin["member_id"],
        "name": admin["name"],
        "role": admin["role"],
        "token": admin["token"],
        "cursor": result["cursor"],
    }
    save_identity(args.alias, identity)
    print_json(
        {
            "saved_as": args.alias,
            "room_id": result["room_id"],
            "admin_member_id": admin["member_id"],
            "invite_token": result["invite_token"],
            "server": args.server,
            "note": "请妥善保存 invite_token；协作者加入时需要它。",
        }
    )


def cmd_join(args):
    payload = {"name": args.name, "invite_token": args.invite}
    path = "/v1/rooms/{}/members/join".format(urllib.parse.quote(args.room, safe=""))
    result = request(args.server, "POST", path, payload)
    identity = {
        "server": args.server,
        "room_id": result["room_id"],
        "member_id": result["member_id"],
        "name": result["name"],
        "role": result["role"],
        "token": result["token"],
        "cursor": result["cursor"],
    }
    save_identity(args.alias, identity)
    print_json(
        {
            "saved_as": args.alias,
            "room_id": result["room_id"],
            "member_id": result["member_id"],
            "name": result["name"],
            "role": result["role"],
        }
    )


def cmd_identities(_args):
    with config_lock():
        items = load_config_unlocked()["identities"]
    safe = {}
    for alias, identity in items.items():
        safe[alias] = {
            key: value
            for key, value in identity.items()
            if key not in ("token",)
        }
    print_json({"identities": safe, "config": str(CONFIG_PATH)})


def cmd_who(args):
    identity = get_identity(args.alias)
    print_json(auth_request(identity, "GET", "/members"))


def cmd_send(args):
    identity = get_identity(args.alias)
    text = args.message or ""
    selected = sum(bool(value) for value in (args.file, args.path))
    if selected > 1:
        raise ClientError("--file 与 --path 不能同时使用")
    payload = {"text": text, "to": args.to}
    if args.file:
        path = Path(args.file).expanduser().resolve()
        if not path.is_file():
            raise ClientError("上传文件不存在或不是普通文件：{}".format(path))
        if path.stat().st_size > 50 * 1024 * 1024:
            raise ClientError("单个文件不能超过 50 MiB")
        with open(path, "rb") as handle:
            content = base64.b64encode(handle.read()).decode("ascii")
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload.update(
            {
                "kind": "image" if mime_type.startswith("image/") else "file",
                "filename": path.name,
                "mime_type": mime_type,
                "content_base64": content,
            }
        )
    elif args.path:
        path = Path(args.path).expanduser().resolve()
        payload.update({"kind": "path", "path": str(path)})
    else:
        if not text:
            raise ClientError("请提供消息文本，或使用 --file/--path")
        payload["kind"] = "text"
    result = auth_request(identity, "POST", "/messages", payload)
    print_json(result)


def cmd_listen(args):
    identity = get_identity(args.alias)
    after = int(identity.get("cursor", 0)) if args.after is None else args.after
    suffix = "/events?after={}&wait={}".format(after, args.wait)
    result = auth_request(identity, "GET", suffix, timeout=args.wait + 10)
    if not args.no_advance:
        update_cursor(args.alias, result["cursor"])
    print_json(result)


def cmd_history(args):
    identity = get_identity(args.alias)
    print_json(auth_request(identity, "GET", "/history?limit={}".format(args.limit)))


def cmd_fetch(args):
    identity = get_identity(args.alias)
    data, headers = auth_request(identity, "GET", "/files/" + args.file_id, raw=True)
    filename = args.output
    if not filename:
        disposition = headers.get("Content-Disposition", "")
        marker = "filename*=UTF-8''"
        if marker in disposition:
            filename = urllib.parse.unquote(disposition.split(marker, 1)[1].split(";", 1)[0])
        else:
            filename = args.file_id
    output = Path(filename).expanduser().resolve()
    if output.exists() and not args.force:
        raise ClientError("输出文件已存在；使用 --force 覆盖：{}".format(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", dir=str(output.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_name, str(output))
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print_json({"saved_to": str(output), "bytes": len(data)})


def cmd_kick(args):
    identity = get_identity(args.alias)
    suffix = "/members/{}/deactivate".format(urllib.parse.quote(args.member_id, safe=""))
    print_json(auth_request(identity, "POST", suffix, {}))


def cmd_rotate_invite(args):
    identity = get_identity(args.alias)
    print_json(auth_request(identity, "POST", "/invite/rotate", {}))


def cmd_doctor(args):
    result = request(args.server, "GET", "/health")
    result["client_config"] = str(CONFIG_PATH)
    print_json(result)


def dashboard_url(identity):
    fragment = urllib.parse.urlencode(
        {"room": identity["room_id"], "token": identity["token"]}
    )
    return "{}/dashboard/#{}".format(identity["server"].rstrip("/"), fragment)


def cmd_dashboard(args):
    identity = get_identity(args.alias)
    if identity.get("role") != "admin":
        raise ClientError("完整聊天监看台仅允许管理员打开")
    url = dashboard_url(identity)
    if args.print_url:
        print(url)
        return
    opened = webbrowser.open(url, new=2)
    if not opened:
        raise ClientError("系统未能打开浏览器；请使用 --print-url 获取地址")
    print_json(
        {
            "opened": True,
            "room_id": identity["room_id"],
            "dashboard": identity["server"].rstrip("/") + "/dashboard/",
        }
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="cgchat",
        description="Codex 本机群组聊天室客户端。默认广播，--to 可定向发送。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("bootstrap", help="创建聊天室并保存管理员身份")
    command.add_argument("--room", default="codex-team")
    command.add_argument("--display-name")
    command.add_argument("--admin-name", default="管理员")
    command.add_argument("--as", dest="alias", default="team-admin")
    command.add_argument("--server", default=DEFAULT_SERVER)
    command.set_defaults(func=cmd_bootstrap)

    command = sub.add_parser("join", help="用邀请令牌加入为协作者")
    command.add_argument("--room", required=True)
    command.add_argument("--invite", required=True)
    command.add_argument("--name", required=True)
    command.add_argument("--as", dest="alias", required=True)
    command.add_argument("--server", default=DEFAULT_SERVER)
    command.set_defaults(func=cmd_join)

    command = sub.add_parser("identities", help="列出本机已保存身份（不显示密钥）")
    command.set_defaults(func=cmd_identities)

    command = sub.add_parser("who", help="列出聊天室成员")
    command.add_argument("--as", dest="alias", required=True)
    command.set_defaults(func=cmd_who)

    command = sub.add_parser("send", help="发送广播或定向消息/文件")
    command.add_argument("--as", dest="alias", required=True)
    command.add_argument("--to", help="收件人的 member_id 或唯一名称；省略则广播")
    command.add_argument("--file", help="上传并发送文件或图片（最多 50 MiB）")
    command.add_argument("--path", help="分享本机文件绝对路径，不复制文件")
    command.add_argument("message", nargs="?", help="消息正文或文件说明")
    command.set_defaults(func=cmd_send)

    command = sub.add_parser("listen", help="长轮询新通知并推进本地游标")
    command.add_argument("--as", dest="alias", required=True)
    command.add_argument("--wait", type=int, default=50, choices=range(0, 56), metavar="0..55")
    command.add_argument("--after", type=int, help="覆盖本地游标")
    command.add_argument("--no-advance", action="store_true")
    command.set_defaults(func=cmd_listen)

    command = sub.add_parser("history", help="读取可见历史；管理员可见全量")
    command.add_argument("--as", dest="alias", required=True)
    command.add_argument("--limit", type=int, default=50, choices=range(1, 501))
    command.set_defaults(func=cmd_history)

    command = sub.add_parser("fetch", help="下载上传型文件")
    command.add_argument("--as", dest="alias", required=True)
    command.add_argument("file_id")
    command.add_argument("--output")
    command.add_argument("--force", action="store_true")
    command.set_defaults(func=cmd_fetch)

    command = sub.add_parser("kick", help="管理员停用协作者")
    command.add_argument("--as", dest="alias", required=True)
    command.add_argument("member_id")
    command.set_defaults(func=cmd_kick)

    command = sub.add_parser("rotate-invite", help="管理员轮换邀请令牌")
    command.add_argument("--as", dest="alias", required=True)
    command.set_defaults(func=cmd_rotate_invite)

    command = sub.add_parser("doctor", help="检查服务健康状态")
    command.add_argument("--server", default=DEFAULT_SERVER)
    command.set_defaults(func=cmd_doctor)

    command = sub.add_parser("dashboard", help="用管理员身份打开实时聊天监看台")
    command.add_argument("--as", dest="alias", default="team-admin")
    command.add_argument(
        "--print-url",
        action="store_true",
        help="输出带临时页面凭据的地址而不自动打开",
    )
    command.set_defaults(func=cmd_dashboard)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ClientError as exc:
        print("cgchat: {}".format(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("cgchat: 已取消", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
