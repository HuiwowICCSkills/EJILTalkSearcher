# EJIL: Talk! 检索实测与结构记录

本文件记录 2026-10-16 对这个独立 skill 的实测结果、站点结构与边界。数字是当日事实，不是承诺；站点改版或 WAF 策略变化后需要重新核验。

## 1. 两条检索路径

| 路径 | 请求 | 能得到什么 | 失败时的表现 |
| --- | --- | --- | --- |
| WordPress REST API（首选） | `GET /wp-json/wp/v2/posts?search=<关键词>&per_page=<n>&offset=<k>&_embed=author` | 精确命中数（响应头 `X-WP-Total`）、页数（`X-WP-TotalPages`）、题名、日期、作者、摘要、**文章正文**（`content.rendered`） | 站点 WAF 可对单次请求返回 HTTP 403；适配器随即改走 HTML 搜索页 |
| HTML 搜索页（回退） | 无头 Chrome 打开 `/?s=<关键词>`（第 N 页为 `/page/N/?s=<关键词>`） | 条目级结果：题名、链接、日期、作者、teaser；页数（如 `Page 1 of 142`） | 只有页数，没有精确命中总数；不返回正文 |

两条路径都不需要登录或 Cookie。

## 2. REST API 实测（2026-10-16）

### 2.1 搜索

请求：`/wp-json/wp/v2/posts?search=war%20crime&per_page=2&_fields=id,link,date_gmt,title,excerpt,content,author`

- HTTP 200；响应头 `X-WP-Total: 1419`、`X-WP-TotalPages: 710`（`per_page=2` 时）。
- `content.rendered` 为站点公开渲染的正文：2026-10-15 的 *The US Sanctions Against the ICC* 为 19,989 字符 HTML；2026-10-15 的阿根廷/UNCLOS 文章为 23,086 字符。
- 加入 `_embed=author` 后，作者名在 `_embedded.author[0].name`（实测 `Marc Weller`）；不加 `_embed` 时只有 `author` 数字 ID。

### 2.2 按 slug 取单篇

请求：`/wp-json/wp/v2/posts?slug=the-us-sanctions-against-the-icc`

- HTTP 200，24,021 字节；正文同样在 `content.rendered`。
- `fetch` 默认先走这条路径，成功时 `mode=api`。

### 2.3 WAF 与查询串编码（重要）

同一天、同一网络条件下实测：

| 请求 | 结果 |
| --- | --- |
| `search=war+crime&per_page=2`（浏览器式 UA） | HTTP 403，重复 4 次均相同 |
| `search=war%20crime&per_page=1`（浏览器式 UA） | HTTP 200 |
| `search=war+crime&per_page=1`（`GILResearch/2.0` 通用 UA） | HTTP 200（同一天、不同 UA） |
| `search=war+crimes&per_page=1` | HTTP 200 |
| `search=armed+conflict&per_page=1`、`search=state+immunity&per_page=2` | HTTP 200 |
| `posts?per_page=2`（不带 search） | HTTP 200 |
| 直接 HTTP 打开文章页 `https://www.ejiltalk.org/the-us-sanctions-against-the-icc/`（浏览器式 UA） | HTTP 403 |

结论与适配器选择：

- 触发 403 的不是“REST API 整体不可用”，而是**逐请求**的拦截；同日同类请求有的 200、有的 403。不能把一次 403 写成“没有资料”。
- 本适配器把空格统一编码成 `%20`（`urllib.parse.urlencode(..., quote_via=quote)`），而不是 `+`，因为实测 `war+crime` 连续 403 时 `war%20crime` 返回 200。
- 设计上不做重试风暴：REST 失败一次即转浏览器回退；回退也失败才整体报错，错误信息保留两层原因。

## 3. HTML 搜索页结构（2026-10-16 实测）

无头 Chrome（空 Cookie）打开 `https://www.ejiltalk.org/?s=war+crime`：

- HTTP 200；标题 `Search Results for “war crime” – EJIL: Talk!`；页面文本含 `Page 1 of 142`；10 张结果卡。
- 第 2 页 `https://www.ejiltalk.org/page/2/?s=war+crime` 返回 `Page 2 of 142`，10 张结果卡，首条为 *The ICC, Arrest Warrants, and Peace Negotiations*。
- 页数与 REST 精确值一致：142 页 × 10 ≈ 1,419 条命中。

适配器依赖的选择器（每项均实测命中 10 次，`section` 1 次）：

| 选择器 | 用途 |
| --- | --- |
| `section.search-page-articles` | 结果区容器；缺失即视为改版或拦截页 |
| `div.post-tease-ejil` | 单张结果卡 |
| `div.post-tease-content-ejil` | 卡片文字区（图片链接也在卡片内，故取链接时必须限定在此容器内） |
| `a.article-link[href]` | 文章链接（图片锚点与题名锚点都用这个类，容器限定后取到题名链接） |
| `h3`（`title` 属性优先） | 题名 |
| `div.featured-date-and-author > p` | 日期 |
| `a.blog-info-author` | 作者（一个卡片可有多个作者） |
| `div.post-tease-content-ejil > p` | teaser |

拦截页判据：结果区缺失且正文出现 `403 / forbidden / just a moment / access denied / human verification` 之一时，报告为访问挑战，不报告为“零结果”。

## 4. 文章页结构（2026-10-16 实测）

无头 Chrome 打开 `https://www.ejiltalk.org/the-us-sanctions-against-the-icc/`：

- 标题 `The US Sanctions Against the ICC – EJIL: Talk!`；正文 HTML 110,689 字节。
- `<article class="article-ejil post-type-post" id="post-27019">`；题名 `h1.blog-info-title`；日期 `time.blog-info-date`（`datetime` 属性同日）；作者 `address.author a`；正文 `div.blog-content`。
- 同页混有评论区（`class="blog-info-author"` 的评论作者卡片等），因此正文解析限定在 `article` 内的 `div.blog-content`，不抓整页文本。

## 5. 边界与未验证项

- HTML 回退的每页条数按站点当前的 10 条/页换算；站点若改版为其它条数，`--offset` 的换算会随之失真，需按上面的选择器表重新核验。
- `--offset` 目前只在 REST 路径按条分页；HTML 回退按页码取页后再按页内偏移切片。
- 站点按 IP / 频率的判断阈值未知；本 skill 不对同一失败请求重试，连续大量检索时仍可能遇到 403/challenge。
- 直接 HTTP 打开文章页在本机 403（走 `fetch` 的 REST 或 headless 路径可绕过该单请求状态），但这不是站点级规则。
