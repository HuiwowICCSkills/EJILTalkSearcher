---
name: EJILTalkSearcher
description: 独立检索 EJIL: Talk!（European Journal of International Law 博客），先走 WordPress REST API，被站点 WAF 拦截时用无头 Chrome 打开站点自身的 HTML 搜索页；返回真实条目、精确命中数或页数，并在可取时返回文章正文。用于查找 EJIL: Talk! 的文章、作者、日期、摘要与全文。
---

# EJILTalkSearcher

> **`<skill-dir>`** = 本 SKILL.md 所在目录。核心脚本为 `<skill-dir>/scripts/ejiltalk_search.py`；Windows 用 `<skill-dir>/run.ps1`，macOS/Linux 用 `<skill-dir>/run.sh`。部署与排错见 [README.md](README.md)。

这个 skill 只做 EJIL: Talk! 一件事：

- `search`：检索博客文章。WordPress REST API 可用时返回精确命中总数、作者和文章正文；REST 被站点 WAF 以 403 拦截时，自动用无头 Chrome 打开站点自身的 `/?s=<关键词>` HTML 搜索页，返回题名、日期、作者和 teaser，只报告页数；
- `fetch`：按文章 URL 取正文，依次尝试 REST slug 查询 → 直接 HTTP 页面 → 无头 Chrome 渲染；
- `doctor`：检查 Python/bs4、Node.js、Chrome 以及站点可达性，判断当前机器至少有一条可用检索路径。

EJIL: Talk! 是公开博客，两条路径都不需要机构登录，也不依赖 `gdufs-auth-session`。

## 命令

```powershell
# 检索（默认 auto：REST API 优先，403 时回退无头浏览器）
& '<skill-dir>/run.ps1' search 'war crime' --limit 10 --out './ejil-search.json'

# 只看 REST API 结果；被 403 拦截时报错，不静默改走浏览器
& '<skill-dir>/run.ps1' search 'state immunity' --mode api --limit 5

# 只用无头浏览器 HTML 搜索页
& '<skill-dir>/run.ps1' search 'aggression' --mode browser --limit 5

# 取一篇文章正文（URL 用 search 返回的条目链接）
& '<skill-dir>/run.ps1' fetch 'https://www.ejiltalk.org/the-us-sanctions-against-the-icc/' --out './ejil-post.json' --text-out './ejil-post.txt'

# 部署自检
& '<skill-dir>/run.ps1' doctor
```

通用参数：`--out`、`--timeout`、`--wait-ms`、`--chrome-path`。`search` 另有 `--limit`（1..50，默认 10）、`--offset`、`--mode auto|api|browser`；`fetch` 另有 `--mode` 和 `--text-out`；`doctor` 可用 `--no-probe` 跳过联网探测。

## 结果边界

- `mode=api` 的结果带 `text`（站点公开渲染的正文）与精确的 `total` / `total_pages`（来自 `X-WP-Total` / `X-WP-TotalPages`）。
- `mode=browser` 是 HTML 搜索页回退：只有题名、日期、作者和 teaser，**不是正文**；只有页数（例如 2026-10-16 实测 `war crime` 为 142 页），没有精确命中总数。需要正文时对条目 URL 再运行 `fetch`。
- 单次 HTTP 403 只是该请求被站点 WAF 拦截，不等于没有资料；不要对同一请求反复重试。`status=error` 的报告保留原始错误，可直接照读。
- `--offset` 在 REST 路径按条翻页；在 HTML 回退路径按站点固定每页 10 条换算页码，优先使用报告里的 `next_offset`。
- 报告里的 `evidence` 记录实际请求 URL、HTTP 状态、字节数和 SHA-256；引用时以 `fetch` 取回的正文为准，不用搜索条目代替正文。

## 参考

- 2026-10-16 实测记录（REST 字段、`%20` 编码与 WAF 现象、HTML 页选择器、页数与正文规模）：[references/ejiltalk-search.md](references/ejiltalk-search.md)
- 部署、运行环境与排错：[README.md](README.md)
