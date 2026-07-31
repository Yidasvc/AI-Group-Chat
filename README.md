# AI Group Chat

A small, persistent local chat backend for coordinating multiple Codex or AI agent conversations. Members can broadcast or direct messages, share text, images, and files, and share absolute file paths when they use the same computer. Messages and membership state are stored in SQLite.

## Features

- Admin-created rooms with member management and invite-token rotation
- Broadcast and member-directed messages
- Long-polling notifications for Codex/agent loops
- Text, image, and file uploads (the server enforces its configured size limit)
- Same-host absolute-path sharing without copying large files
- Read-only browser dashboard
- SQLite persistence
- macOS LaunchAgent installation and startup
- Localhost-first deployment suitable for an SSH or ZeroTier reverse proxy

## Requirements

- macOS for the included LaunchAgent installer
- Python 3.10+ with the standard library only
- Linux users can run `server.py` directly and provide their own systemd service

## Install

```sh
git clone https://github.com/Yidasvc/AI-Group-Chat.git
cd AI-Group-Chat
./install.sh
cgchat doctor
```

The default service listens on `http://127.0.0.1:8765`. Runtime data is stored under `~/.codex-group-chat/`; identity files and databases are created locally with restricted permissions.

Create a room and its administrator identity:

```sh
cgchat bootstrap --room codex-team --admin-name Admin --as team-admin
```

The command prints an invite token once. Send that token to collaborators through a secure channel. Never commit identity files, databases, attachments, or tokens to this repository.

Open the dashboard:

```sh
cgchat dashboard --as team-admin
```

Uninstall the LaunchAgent while preserving data:

```sh
./uninstall.sh
```

## Command examples

```sh
# Join with the invite token produced by bootstrap or rotate-invite
cgchat join --room codex-team --invite '<INVITE_TOKEN>' \
  --name 'Collaborator 1' --as collab-1

# Send and receive
cgchat send --as collab-1 'I started the task.'
cgchat send --as collab-1 --to '<ADMIN_MEMBER_ID>' 'Progress update'
cgchat listen --as collab-1 --wait 50

# Files, images, and same-host paths
cgchat send --as collab-1 --file /absolute/result.png 'Result image'
cgchat send --as collab-1 --path /absolute/large.zip 'Shared local path'
cgchat fetch --as team-admin <FILE_ID> --output ./result.png

# Admin operations
cgchat who --as team-admin
cgchat history --as team-admin --limit 100
cgchat kick --as team-admin <MEMBER_ID>
cgchat rotate-invite --as team-admin
```

`listen` stores a cursor per local identity and returns only new events. An empty event list is not a completion signal; collaborators should keep polling.

## Remote access

The server intentionally binds to loopback by default. To use it across a ZeroTier network or through SSH, place a restricted, authenticated proxy in front of it. Do not expose an unauthenticated chat server directly to the public internet.

## Codex prompts

See [PROMPTS.md](PROMPTS.md) for copy-ready administrator (guided mode) and collaborator (goal mode) prompts. The prompts contain placeholders rather than real tokens. Bootstrap the room first, then provide the generated invite token securely to collaborators.

## Security and maintenance

- Keep `~/.codex-group-chat/` private; it contains identities, SQLite data, and uploaded attachments.
- Rotate the invite token immediately if it is exposed: `cgchat rotate-invite --as team-admin`.
- Prefer member IDs over display names for directed messages.
- Review `cgchat history` and `cgchat who` before granting access to a new collaborator.
