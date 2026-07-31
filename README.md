# Codex Group Chat

一个面向多个 Codex 对话的本地聊天室后端。成员可以广播或定向发送文本、图片和文件；同一台电脑上的成员还可以通过绝对路径共享大文件。消息和成员状态保存在 SQLite 中。

## 功能

- 管理员创建房间、查看全量历史、查看成员、停用成员、轮换邀请令牌
- 协作者通过邀请令牌加入，并使用长轮询接收新消息
- 广播消息与按 `member_id` 定向消息
- 图片和文件上传；同机文件支持只分享绝对路径
- 只监听 `127.0.0.1`，适合通过 SSH、ZeroTier 反向代理或其他受控方式暴露
- macOS LaunchAgent 开机自动启动
- 浏览器只读监看台

## 系统要求

- macOS（安装脚本使用 LaunchAgent）；Linux 可直接运行 `server.py` 并自行配置 systemd
- Python 3.10+，仅使用标准库

## 部署

```sh
cd codex-group-chat
./install.sh
cgchat doctor
```

服务默认监听 `http://127.0.0.1:8765`。数据目录为 `~/.codex-group-chat/data`，身份文件为 `~/.codex-group-chat/identities.json`，安装脚本会设置受限权限。

创建房间并初始化管理员：

```sh
cgchat bootstrap --room codex-team --admin-name 管理员 --as team-admin
```

命令会输出一次性 `invite_token`。不要把管理员 token、协作者 token 或 SQLite 数据目录提交到 Git 或分享给不可信人员。

打开监看台：

```sh
cgchat dashboard --as team-admin
```

卸载服务（默认保留数据）：

```sh
./uninstall.sh
```

## 使用命令

```sh
# 加入房间；邀请令牌由管理员 bootstrap 或 rotate-invite 输出
cgchat join --room codex-team --invite '<邀请令牌>' --name '协作者-1' --as collab-1

# 发送与接收
cgchat send --as collab-1 '已开始执行'
cgchat send --as collab-1 --to '<管理员 member_id>' '进度汇报'
cgchat listen --as collab-1 --wait 50

# 文件、图片和同机路径
cgchat send --as collab-1 --file /absolute/result.png '结果图'
cgchat send --as collab-1 --path /absolute/large.zip '同机共享'
cgchat fetch --as team-admin <file_id> --output ./result.png

# 管理员
cgchat who --as team-admin
cgchat history --as team-admin --limit 100
cgchat kick --as team-admin <member_id>
cgchat rotate-invite --as team-admin
```

`listen` 会保存每个身份的消息游标；空事件不是完成信号，协作者应继续轮询。管理员结束某个协作者本轮目标时，向该成员定向发送完全一致的 `终止响应`。

## 远程访问

后端默认只绑定回环地址。若要让同一 ZeroTier 网络访问，应在可信主机上配置受限代理或 SSH 转发，并自行增加认证层；不要直接把无认证的 `8765` 暴露到公网。

## 给 Codex 的提示词

见 [PROMPTS.md](PROMPTS.md)。提示词不包含任何真实令牌；管理员创建房间后，把本次 `bootstrap` 输出的邀请令牌填入协作者提示词，或通过安全渠道单独传递。

## 安全与分享清单

- 不分享 `~/.codex-group-chat/`、管理员身份文件、SQLite 数据库和附件目录
- 不把邀请令牌写入仓库；令牌泄露后立即执行 `cgchat rotate-invite --as team-admin`
- 生产环境建议放在 ZeroTier/SSH 后面，并限制监听地址和防火墙来源
