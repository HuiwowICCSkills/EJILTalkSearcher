# EJILTalkSearcher 部署指引

独立的 EJIL: Talk! 检索 skill：博客文章检索、按 URL 取全文、部署自检。EJIL: Talk! 是公开博客，本 skill 不需要机构登录、不保存任何凭据，也不写入 skill 目录以外的任何文件。

仓库：<https://github.com/HuiwowICCSkills/EJILTalkSearcher>（公开仓库）。

## 1. 运行环境

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| Python | 3.10+ | 唯一第三方依赖是 `beautifulsoup4` |
| Node.js | 22+ | 只有无头浏览器回退需要（全局 `WebSocket` API）；`doctor` 会单独列出 |
| 浏览器 | Chrome / Chromium / Edge | 只有无头浏览器回退需要；可用 `CHROME_PATH` 或 `--chrome-path` 指定 |
| 网络 | 可访问 `www.ejiltalk.org` | REST API 与 HTML 搜索页都指向同一站点 |

只有 `search --mode api` / `fetch --mode api` 是纯 Python 路径；默认的 `auto` 模式在 REST 被 403 拦截时会调用 Node.js 与 Chrome，因此建议把四项都装齐。

## 2. 安装

```powershell
git clone https://github.com/HuiwowICCSkills/EJILTalkSearcher.git "$env:USERPROFILE\.codex\skills\EJILTalkSearcher"
python -m pip install -r "$env:USERPROFILE\.codex\skills\EJILTalkSearcher\requirements.txt"
```

Codex 用户 `<skills-root>` 通常是 `%USERPROFILE%\.codex\skills`（macOS/Linux 为 `~/.codex/skills`）。`run.ps1` / `run.sh` 会依次探测 `venv/`、随附运行时和 PATH 上的 Python，并挑选第一个能 `import bs4` 的解释器；没有可用解释器时会直接报错并提示安装依赖。

## 3. 自检

```powershell
& "$env:USERPROFILE\.codex\skills\EJILTalkSearcher\run.ps1" doctor
```

`doctor` 逐项检查 Python、beautifulsoup4、Node.js（22+）、Chrome，并各发一次站点探测（站点根页 + REST API）。整体 `ok: true` 的含义是：**至少有一条可用检索路径**——要么 REST API 探到 2xx，要么 Node.js、Chrome、站点根页都正常（可走无头浏览器回退）。`--no-probe` 可跳过联网探测。

## 4. 使用

```powershell
$ejil = "$env:USERPROFILE\.codex\skills\EJILTalkSearcher\run.ps1"

# 默认 auto：REST API 优先，被 403 拦截时回退无头浏览器的 HTML 搜索页
& $ejil search 'war crime' --limit 10 --out './ejil-search.json'

# 只允许 REST API（403 会作为错误报告，不会静默给出空结果）
& $ejil search 'state immunity' --mode api --limit 5

# 只允许无头浏览器 HTML 搜索页（条目级结果：题名/日期/作者/teaser）
& $ejil search 'aggression' --mode browser --limit 5

# 取单篇正文；--text-out 另存纯文本
& $ejil fetch 'https://www.ejiltalk.org/the-us-sanctions-against-the-icc/' --out './ejil-post.json' --text-out './ejil-post.txt'
```

不加 `--out` 时报告直接打印到标准输出。`--limit` 取 1..50；`--timeout` 取 1..1800 秒；`--wait-ms` 取 0..120000，是无头浏览器导航后的等待时间。

报告的关键字段：

| 字段 | 含义 |
| --- | --- |
| `mode` | `api`（REST API）或 `browser`（HTML 搜索页回退）；`fetch` 还可能是 `page`（直接 HTTP 页面）或 `headless` |
| `total` / `total_pages` | REST 路径为精确值；`browser` 路径的 `total` 恒为 `null`，只给页数 |
| `results[].text` | REST 路径下是站点公开渲染的正文；`browser` 路径下为空 |
| `next_offset` | 下一页偏移；`null` 表示已到最后一页 |
| `warnings` | 本次实际发生的降级说明（例如 REST 被 403、改用浏览器回退） |
| `evidence` | 实际请求 URL、HTTP 状态、字节数、SHA-256、抓取时间 |

## 5. 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `status=error`，`error` 含 `HTTP 403` | 该次请求被站点 WAF 拦截 | 这是单次请求的状态，不代表没有资料；默认 `auto` 模式会自动改走浏览器回退，`--mode api` 时才会直接报错 |
| 结果来自 `mode=browser` 且 `text` 为空 | HTML 搜索页只提供条目元数据与 teaser | 对需要的条目 URL 运行 `fetch` 取正文 |
| `Node.js is required for the headless-browser fallback` | 未安装 Node.js 或不在 PATH | 安装 Node.js 22+ |
| `Chrome/Chromium not found` | 未安装浏览器 | 安装 Chrome/Edge，或设置 `CHROME_PATH` / 传 `--chrome-path` |
| `Headless browser timed out` | 页面加载慢于 `--timeout` | 提高 `--timeout`（该值同时约束导航与渲染），或稍后重试 |
| `HTML search schema changed` | 站点改版，选择器失效 | 别把它当零结果；按 [references/ejiltalk-search.md](references/ejiltalk-search.md) 的选择器表核对 `section.search-page-articles` 等结构后更新适配器 |
| `No Python with beautifulsoup4 was found` | 解释器缺依赖 | `python -m pip install -r requirements.txt` |

## 6. 边界与合规

- 只做公开页面检索，不绕过验证码、付费墙或访问控制；目标站明确拒绝自动化访问时报告限制。
- 单次 403 不等于资料不存在，也不代表站点整体不可用；不要对同一请求反复探测。
- `browser` 回退的条目（题名/日期/作者/teaser）是元数据，不是全文；引用时用 `fetch` 返回的正文，并保留 `evidence` 中的 URL 与哈希。
- 报告中的 `body_html` 保留站点原始标记；纯文本字段 `text` 只是去标签后的同源文本，不改变措辞。

## 7. 更新

```powershell
git -C "$env:USERPROFILE\.codex\skills\EJILTalkSearcher" pull
```

本 skill 不依赖同组织其它仓库；`gdufs-auth-session` / `HeinOnlineSearcher` 与其互不影响。

## 实测效果（2026-11-09）

以下命令与结果来自本机 `~/.codex/skills/EJILTalkSearcher` 的实网运行；检索日期 2026-11-09（Asia/Shanghai），输出为该次运行实际返回的 JSON 报告。

```powershell
$ejil = "$env:USERPROFILE\.codex\skills\EJILTalkSearcher\run.ps1"
& $ejil search 'US sanctions ICC' --limit 5
```

- 状态：`ok`；`mode=api`；服务端报告 `total = 151`、`total_pages = 31`；本次返回 5 条。
- `scope` 原文：`native WordPress post search; total and total_pages are the exact X-WP-Total / X-WP-TotalPages values`。
- 代表性结果：

| 题名 | 类型 / 日期 | 作者 | 链接 |
| --- | --- | --- | --- |
| Two Weeks in Review: 19 October—1 November 2026 | retrieved_text；11/01/2026 12:00:50 | Sebastian von Massow | https://www.ejiltalk.org/two-weeks-in-review-19-october-1-november-2026/ |
| Announcements: US Sanctions Against the ICC Roundtable; CfP Battlefield Forensics; EU-Mercosur Partnership Agreement Workshop; CfS ASIL International Legal Theory Prize; CfP Sustainability and its Discontents; Security Concerns in International Law Workshop; CfS International Criminal Law Scholarship Prize; Governing AI in a Fragmented World | retrieved_text；11/01/2026 10:00:27 | Mary Guest | https://www.ejiltalk.org/announcements-us-sanctions-against-the-icc-roundtable-cfp-battlefield-forensics-eu-mercosur-partnership-agreement-workshop-cfs-asil-international-legal-theory-prize-cfp-sustainability-and-its-dis/ |
| EJIL: The Podcast! Episode 47: Meeting the Moment: ICC Sanctions and the Future of International Adjudication | retrieved_text；10/26/2026 08:00:45 | Leena Grover | https://www.ejiltalk.org/ejil-the-podcast-episode-47-meeting-the-moment-icc-sanctions-and-the-future-of-international-adjudication/ |
| Two Weeks in Review: 5—16 October 2026 | retrieved_text；10/18/2026 11:00:01 | Sebastian von Massow | https://www.ejiltalk.org/two-weeks-in-review-5-16-october-2026/ |
| The US Sanctions Against the ICC | retrieved_text；10/15/2026 12:00:01 | Marc Weller | https://www.ejiltalk.org/the-us-sanctions-against-the-icc/ |

> 说明：结果类型为 `retrieved_text`，即通过 WordPress REST `posts` 接口返回的公开正文（`content_scope` 原文：`public rendered post body via the WordPress REST posts API; protected posts may omit content`），本次各条 `text_characters` 为 5546、7213、3381、9732、15322 字符，因此可当作正文使用，而不只是摘要。但这仍是站点自身的正文文本，不等于经外部核验的引证；引用时保留 `evidence` 中的 URL 与 SHA-256，并注意受保护文章可能缺正文。`total=151` 是 WordPress 索引的精确命中数，不是本次已核验文献数。
