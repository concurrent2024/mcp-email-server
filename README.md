# mcp-email-server

一个能**收发邮件**的 MCP 服务器。通过标准 IMAP 读信、标准 SMTP 发信，因此 Gmail、QQ、163、Outlook、企业自建邮箱都能接入，不依赖任何单一服务商的私有 API。

用 Python 编写，基于 [MCP Python SDK v2](https://py.sdk.modelcontextprotocol.io/)。

## 它能做什么

**读信**

| 工具 | 作用 |
| --- | --- |
| `search_emails` | 按发件人、主题、正文、日期、未读状态搜索，返回简要列表 |
| `read_email` | 读取单封邮件全文、头信息与附件清单 |
| `wait_for_new_emails` | 阻塞等待新邮件到达（例如等验证码），超时返回 |
| `list_folders` | 列出邮箱文件夹及其特殊用途标记 |
| `download_attachment` | 把附件保存到本地目录 |

**写信**

| 工具 | 作用 |
| --- | --- |
| `send_email` | 发送邮件，支持抄送、密送、HTML、附件、回复原邮件 |
| `save_draft` | 只存草稿不发送，交给人工在自己的客户端里过目 |

**管理与诊断**

| 工具 | 作用 |
| --- | --- |
| `mark_email` | 标记已读/未读、加星/取消 |
| `move_email` | 移动到其他文件夹 |
| `delete_email` | 移入垃圾箱（是移动不是彻底删除，可恢复） |
| `check_connection` | 分别验证 IMAP 与 SMTP 是否可用，且绝不回显密码 |

此外还提供资源 `email://{文件夹}/{uid}`（把某封邮件当作上下文附件挂进对话），以及提示词 `draft_reply`（带原文引用地起草回复）。

## 安装

需要 Python 3.10 及以上。

```bash
git clone <this-repo> && cd mcp-email-server
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 配置

复制 `.env.example` 为 `.env` 并填写，或者直接用环境变量。

```bash
cp .env.example .env
```

最小可用配置：

```bash
IMAP_HOST=imap.qq.com
IMAP_USERNAME=you@qq.com
IMAP_PASSWORD=你的授权码
SMTP_HOST=smtp.qq.com
EMAIL_FROM=you@qq.com
EMAIL_ALLOW_SEND=true
```

填完先自检，不用启动客户端：

```bash
.venv/bin/mcp-email-server --check
```

它会分别连接 IMAP 与 SMTP 并报告结果，成功时退出码为 0。

### 各邮箱服务商参数

| 服务商 | IMAP | SMTP | 密码填什么 |
| --- | --- | --- | --- |
| Gmail | `imap.gmail.com:993` ssl | `smtp.gmail.com:465` ssl | 应用专用密码（需先开两步验证） |
| QQ 邮箱 | `imap.qq.com:993` ssl | `smtp.qq.com:465` ssl | 授权码（设置 → 账户 → 开启 IMAP/SMTP 服务） |
| 163 / 126 | `imap.163.com:993` ssl | `smtp.163.com:465` ssl | 客户端授权密码（设置 → POP3/SMTP/IMAP） |
| Outlook / M365 | `outlook.office365.com:993` ssl | `smtp.office365.com:587` starttls | 见下方说明 |
| 自建（Dovecot 等） | `mail.example.com:993` ssl | `mail.example.com:587` starttls | 账号密码 |

几个务必注意的点：

- **绝大多数服务商不接受登录密码**。Gmail、QQ、163 都要求单独生成一串“授权码”或“应用专用密码”，填错了会得到一条含糊的登录失败信息。
- **Microsoft 已停用个人 Outlook.com 与多数 M365 租户的基本认证（basic auth）**。也就是说密码方式在那里多半已经行不通，需要 OAuth2 —— 本项目当前尚未实现完整的 OAuth2 授权流程（见下文“扩展 OAuth2”）。
- **163/126 需要 IMAP `ID` 命令**，否则登录后每条命令都会被拒绝并返回 `Unsafe Login`。本服务器会在服务端声明支持时自动发送该命令，你不需要做任何事。
- `SMTP_USERNAME` / `SMTP_PASSWORD` 留空时自动复用 `IMAP_*` 的值，同一个账号不必填两遍。

### 安全开关

默认配置是保守的：**装好之后只能读信，不能发信也不能删信**。这是刻意的——模型可能误解意图，而发出去的邮件收不回来。

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `EMAIL_ALLOW_SEND` | `false` | 不显式打开就无法发信 |
| `EMAIL_ALLOW_DELETE` | `false` | 不显式打开就无法移动或删除邮件 |
| `EMAIL_RECIPIENT_ALLOWLIST` | 空（放行所有人） | 收件人白名单，可写完整地址或域名 |
| `EMAIL_ATTACHMENT_DIR` | `./attachments` | 附件读写只允许发生在此目录内 |
| `EMAIL_MAX_BODY_CHARS` | `20000` | 单封正文返回上限，避免撑爆模型上下文 |
| `EMAIL_SAVE_SENT_COPY` | `false` | 发信后往“已发送”追加一份副本 |

刚接入时**强烈建议先把白名单设成你自己的地址**，确认整条链路行为符合预期后再放开：

```bash
EMAIL_RECIPIENT_ALLOWLIST=me@example.com
```

白名单支持三种写法：完整地址 `bob@example.com`、域名 `@example.com` 或 `example.com`。匹配的是真实地址，不是显示名，所以 `me@example.com <attacker@evil.com>` 这类伪装会被拒绝。

关于 `EMAIL_SAVE_SENT_COPY`：Gmail 会在服务端自动保存通过 SMTP 发出的邮件，而 QQ、163 等大多数服务商不会——在那些邮箱上如果不打开这个开关，你发出去的信除了对方收件箱之外哪儿都不存在。

## 接入 Cursor

在项目里建 `.cursor/mcp.json`（或用户级 `~/.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "email": {
      "command": "/绝对路径/mcp-email-server/.venv/bin/mcp-email-server",
      "env": {
        "IMAP_HOST": "imap.qq.com",
        "IMAP_USERNAME": "you@qq.com",
        "IMAP_PASSWORD": "你的授权码",
        "SMTP_HOST": "smtp.qq.com",
        "EMAIL_FROM": "you@qq.com",
        "EMAIL_ALLOW_SEND": "true",
        "EMAIL_RECIPIENT_ALLOWLIST": "you@qq.com"
      }
    }
  }
}
```

Claude Desktop 的 `claude_desktop_config.json` 格式相同。

如果不想把密码写进配置文件，可以省略 `env`，改为在项目根目录放一个 `.env`——服务器启动时会自动读取。此时记得把 `.env` 加入 `.gitignore`（本仓库已经加了）。

想通过 HTTP 而不是 stdio 提供服务：

```bash
mcp-email-server --transport streamable-http --host 127.0.0.1 --port 8000
```

注意 HTTP 模式没有内置鉴权，只应绑定在本机或可信网络内。

## 用起来是什么样

配好之后直接用自然语言指挥即可：

- “看看我收件箱里有没有来自财务的未读邮件” → `search_emails`
- “把第二封的完整内容念给我听” → `read_email`
- “帮我回复她，说方案我同意，周五之前给她初稿” → `draft_reply` / `send_email`（带 `reply_to_uid`，回复会正确挂在原会话线程上）
- “等一下验证码邮件，收到告诉我” → `wait_for_new_emails`
- “把那封带发票的附件下载下来” → `download_attachment`

服务器的 instructions 里明确要求模型在调用 `send_email` 前先把收件人、主题、正文摆给你确认，各工具也带了 `readOnlyHint` / `destructiveHint` 标注，支持的客户端会据此决定是否弹出确认框。但这些是提示而非强制——真正的兜底是上面那几个开关和白名单。

## 开发

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

测试不需要任何真实邮箱账号：

- SMTP 发信路径用 `aiosmtpd` 起一个本地真实 SMTP 服务（含 AUTH），断言信封收件人、MIME 结构、附件内容、中文主题的 RFC 2047 编码。
- IMAP 路径用真实构造的 RFC 5322 报文喂给假 mailbox，覆盖编码头解码、HTML-only 正文降级、超长截断、附件文件名消毒。
- 工具契约用 SDK 自带的内存客户端 `Client(mcp)` 直连服务器对象，不起进程不占端口。
- 安全策略单独覆盖：开关关闭时必须拒绝、白名单外必须拒绝、错误信息里绝不出现密码。

用 MCP Inspector 手动点一遍（需要本机有 Node.js）：

```bash
npx @modelcontextprotocol/inspector .venv/bin/mcp-email-server
```

这里直接把 Inspector 指向安装好的可执行文件，而不是用 `mcp dev src/mcp_email/server.py`——后者会按文件路径加载模块，包内的相对导入会因此失效。

代码检查：

```bash
.venv/bin/ruff check src tests
```

## 代码结构

```
src/mcp_email/
  config.py       # 环境变量配置与安全策略（白名单、路径限制、密码脱敏）
  auth.py         # AuthProvider 抽象：PasswordAuth，以及 XOAuth2Auth 的骨架
  models.py       # 工具返回的 Pydantic 模型，也就是对模型暴露的契约
  imap_client.py  # 收信：连接、搜索、解析、标记、移动、附件、轮询新邮件
  smtp_client.py  # 发信：MIME 组装与投递
  server.py       # MCPServer：全部工具、资源与提示词
  __main__.py     # CLI 入口
```

每次 IMAP 操作都是“连接 → 干活 → 登出”。IMAP 服务器会主动断开空闲连接，而一个 MCP 服务器可能几小时都没人调用，长连接放在那儿多半已经死了。

## 扩展 OAuth2

`auth.py` 里的 `AuthProvider` 就是为此准备的：`imap_client` 和 `smtp_client` 从不直接接触密码，只调用 `authenticate_imap` / `authenticate_smtp`。

`XOAuth2Auth` 已经写好了两侧的 SASL XOAUTH2 报文格式，缺的只是令牌本身——给它一个能返回 access token 的 `token_provider` 就能工作。还没做的是外围的授权流程：拿到 refresh token、刷新、本地缓存。这部分完成后，`build_auth()` 里加一个分支即可，IMAP 与 SMTP 客户端一行都不用改。

## 已知限制

- 等待新邮件用的是轮询而非 IMAP IDLE 推送。`imaplib` 的 `idle()` 要 Python 3.13 才有，而轮询对所有服务商都可用；代价是最长有一个轮询间隔的延迟。
- HTML 邮件转纯文本是基于标准库 `HTMLParser` 的朴素实现，能保留段落和列表的换行，但复杂排版（表格布局、内联样式）会被压平。
- 尚未实现 OAuth2 授权流程，因此接不了已停用基本认证的 Microsoft 账号。

## 许可

MIT
