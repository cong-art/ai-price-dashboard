# 模型价格台 · 活数据版

把画布上的「模型价格台」变成真正能用的工具：抓取公开价格 API、自动累积历史、定时刷新；浏览器侧表格动态加载活数据。

## 目录结构

```
ai-price-dashboard/
├── index.html                  # 单文件应用，浏览器直接打开（或经本地服务）
├── .env.example                # 历史外部持久化（GitHub）的环境变量配置示例
├── data/
│   ├── models.json             # 模型清单 + 厂商官网定价页 URL 映射
│   ├── baseline.json           # 接入 OpenRouter 前的画布静态快照（仅供首日播种）
│   ├── prices.json             # 价格活数据快照（fetch-prices.py 生成，HTML 加载它）
│   ├── history.json            # 价格历史采样本地缓存副本（权威源见下方「历史外部持久化」）
│   └── news.json               # 资讯活数据快照（fetch-news.py 生成，HTML 加载它）
└── scripts/
    ├── fetch-prices.py         # 价格抓取：拉 OpenRouter/官网官方价 → prices.json + history（含可复用 refresh()）
    ├── fetch-news.py           # 资讯抓取：IT之家+量子位 RSS → 过滤/去重/分类 → news.json（含可复用 refresh()）
    ├── gh-history.py           # GitHub 私有仓库历史持久化后端：把 history 样本存到外部仓库文件
    ├── server.py               # 线上自更新 HTTP 服务：静态托管 + 新闻每日 08:00 起惰性自刷新
    └── seed-history.py          # 一次性的种子脚本：用 baseline 注入 3 个历史样本
```

## 一次性初始化

```
# 1. 注入历史种子（让首日即有「较昨日/较上周/较上月」对比可看）
python scripts/seed-history.py

# 2. 立即跑一次抓取（写入 prices.json）
python scripts/fetch-prices.py
```

之后无需再跑 seed；后续由定时任务自动积累真实历史。

## 定时任务

已注册两个 Windows 计划任务：

| 任务 | 频率 | 动作 |
|---|---|---|
| `ModelPriceBoard-Fetch` | 每 6 小时 | 抓价格 → `prices.json` + `history.json`（维持「较昨日/较上周/较上月」对比所需的频繁采样）|
| `ModelPriceBoard-News` | 每天 08:00 | 抓中文 AI 资讯 → `news.json`，让看板每天早上呈现前一天的最新新闻 |

```powershell
# 查看两个任务
Get-ScheduledTask -TaskName "ModelPriceBoard-*" | Get-ScheduledTaskInfo

# 立即手动触发一次
Start-ScheduledTask -TaskName "ModelPriceBoard-Fetch"
Start-ScheduledTask -TaskName "ModelPriceBoard-News"

# 暂停 / 恢复 / 卸载
Disable-ScheduledTask -TaskName "ModelPriceBoard-News"
Enable-ScheduledTask  -TaskName "ModelPriceBoard-News"
Unregister-ScheduledTask -TaskName "ModelPriceBoard-News"
```

任务使用与本机一致的 Python 路径，工作目录已设为 `ai-price-dashboard/`。

## 数据源说明

- **抓取来源**：OpenRouter `/api/v1/models`（免费、免 Key、浏览器直连支持 CORS）
- **官网官方价 override**：部分模型在 `data/models.json` 里带 `official` 字段，抓取时**跳过 OpenRouter、直接采用厂商官方标价**（`src="official"`），并记录在 `note` 中。当前 5 款走官方价：
  - GPT-5.6 Sol（官网标准档短上下文 $4.00/$20.00，促销价至少至 2026-11-21）
  - GLM-5.3（官网 USD $1.40/$4.40/缓存 $0.26；人民币刊例 ¥8/¥28/¥2 每百万）
  - DeepSeek V4 Pro / V4 Flash（官网峰谷计价：Pro $1.32/$3.96/缓存 $0.044，Flash $0.44/$1.32/缓存 $0.014）
  - Gemini 3.6 Flash（官网 intro 促销价 $0.75/$3.75/缓存 $0.075，2027-01-01 恢复 $1.50/$7.50/$0.15）
- **列结构（2026-09-03 改版）**：价格表移除「混合成本」列，改为「缓存输入」列；对官方区分闲时/高峰的模型（DeepSeek V4 Pro/Flash、Gemini 3.6 Flash Flex），输入/输出/缓存三格均以「高峰主价 + 闲时半价副价」双行呈现。GLM-5.3 无分时计价，Coding Plan 订阅错峰折扣以调价说明注明。
- **与厂商官方口径核对（2026-09-03 复核）**：Gemini 3.1 Pro、GPT-5.6 Terra、GPT-5.6 Luna、Grok 4.6 等 OpenRouter 价与各官网标准价一致；Gemini 3.6 Flash 与 DeepSeek 缓存价已按官网刊例换算为 USD；官方 override 行的历史以独立 key 记录，与 OpenRouter 采样隔离，切换口径不会产生虚假涨跌幅。
- **官网跳转**：表格每行右侧「官网 ↗」按钮跳转对应厂商官方定价页，URL 见 `data/models.json` 的 `vendors` 映射

### 新模型自动发现（2026-09-07 新增）

每次抓取价格时，同时比对 OpenRouter 全目录，**自动发现新发布的模型并收录进价格表**：

- **判定口径**：最近 21 天内上架（OpenRouter `created` 字段）+ 作者前缀在重点厂商白名单（OpenAI / Anthropic / Google / 深度求索 / 月之暗面 / 智谱 / 阿里 / xAI / MiniMax）；排除免费档、计费变体（`:batch`/`:free`/`:beta` 等）与非对话模型（embed/tts/veo 等）
- **版本快照归并**：同一模型的新版本快照（如 `qwen3.8-max-0902`）不另立条目，自动归并为既有模型的 `fallbacks`（老 id 下架后无缝切换）
- **入库**：新模型写入 `data/models.json`（带 `discovered` 日期与「自动发现」note），按混合成本自动分组（旗舰/主力/性价比）；已见模型 id 登记在 `data/discovered.json` 防重复判定；单次最多收录 5 款
- **前端渲染**：静态快照之外的模型行由页面动态构建插入对应分组，模型名带绿色「新」徽标；副标题模型数/厂商数随数据动态更新


## 新闻数据源说明

- **来源（2026-09-03 定）**：IT之家 `/rss/` + 量子位 `/feed`（均为标准 RSS，稳定可达）。原拟的 36氪 `/feed` 被反爬拦截（返回 HTML 挑战页），已弃用；量子位为垂直 AI 媒体，作主力源。
- **过滤口径**：相关性判断**仅依据标题**做保守白名单匹配——RSS 正文（description）里"AI/机器人"常作泛背景词出现，若混入会淹没信号（手机/汽车/硬件新闻几乎都会提一句 AI），故不参与判断。
- **清洗**：摘要剥离「IT之家 X月X日消息，据 XX 报道」式开头引导语与结尾残句，截断到合理长度。
- **分类**：按标题关键词自动打标（融资并购 / 监管动态 / 巨头动态 / 智能应用 / 算力硬件 / 开发者研究 / 产业合作 / 行业数据 / 模型动态），兜底「AI 产业」。
- **去重排序**：跨源标题归一化去重，按发布时间倒序，取最新最多 9 条。
- **渲染**：index.html 优先加载 `data/news.json` 动态重建 9 张卡片并刷新日期副标题；file:// 或文件缺失时回退到内嵌静态精选卡片。
- **原文跳转**：每条新闻自带 RSS 中的原文 `link`，动态卡片整卡渲染为 `<a>`（新标签打开、`rel="noopener noreferrer"`），hover 时卡片微亮、标题变链接蓝并显示「阅读原文 ↗」；无链接条目回退普通卡片。file:// 静态兜底卡片无真实原文 URL，不可点击。

## 浏览器使用

直接双击 `index.html` 打开可见静态快照（`file://` 协议 fetch 受限，无法加载 prices.json）。

推荐通过本地 HTTP 服务打开，JS 才会真正加载 prices.json：

```bash
cd ai-price-dashboard
python -m http.server 8000
# 浏览器打开 http://localhost:8000/
```

页面加载完成后会看到：
- 顶部时间戳更新为活数据生成时间
- 表格副标题更新为「OpenRouter 实时挂牌价」
- 每行按 模型/输入/输出/缓存输入 + 较昨日/上周/上月 + 调价说明 更新；带闲时档模型显示「主价+闲副价」双行，涨红跌绿
- 「模型能力雷达」内嵌综合分前 6 的单模型六维雷达图（编码/推理/长上下文/多模态/指令遵循/性价比），其余模型以链接 chips 呈现、点击弹层查看其雷达；评分数据存于 `data/models.json` 各模型的 `radar` 字段，页面加载时动态选取——**模型增删后雷达区自动重排**（新模型综合分靠前即进内嵌位，被挤出者落入点击序列，30 天内发现的模型带「新」徽章）

## 线上自更新服务（公网链接用）

> 背景：本机 `fetch-news.py`（每天 08:00）与 `fetch-prices.py`（每 6 小时）由 Windows 计划任务写本地 `data/*.json`，但**部署出去的静态快照只是上传那一刻的拷贝**，不会自己变。要公网链接也做到「每天自动更新」，需让**线上服务自己定时抓取**。

**`scripts/server.py`** 同时承担两个角色：
1. **静态托管**：把 `index.html` / `data/*.json` serve 出去；对 `data/news.json`、`data/prices.json`、`data/history.json` 统一加 `Cache-Control: no-cache, no-store`，杜绝浏览器 HTTP 强缓存导致「看到的还是昨天」。
2. **每日自更新**：进程内记录各类最后一次成功刷新的日期；**每次收到请求惰性检查**——若已过该类每日刷新时刻且当天还没刷过，就抓一次（原子写）。无需外部定时器 / cron，只要有人访问或服务常驻即自动补齐，当天其余请求命中缓存不再重复抓。
   - **新闻**：每天 **08:00** 起刷新 → 前一天 + 当日早上最新 AI 资讯
   - **价格**：每天 **09:00** 起刷新 → 当日最新挂牌价（同时追加一条历史采样）

```bash
# 本地跑（PORT 可省略，默认 8000）
PORT=8712 python scripts/server.py --refresh-once   # --refresh-once: 启动即先刷新闻+价格，保证首访最新
# 浏览器打开 http://localhost:8712/
```

监听 `$PORT` 并绑定 `0.0.0.0`，因此可直接作为发布用的 HTTP 服务。

**抓取健壮性**（fetch-prices.py）：OpenRouter 拉取失败自动重试一次；仍失败或条目缺失时**沿用上次快照**（payload 标记 `carried`），绝不让一次网络抖动把 16 模型的完整数据覆盖成残缺（此前的「null/—」单元格即源于此）；`prices.json` / `history.json` 均为原子写（.tmp → os.replace），读端不会抓到半截文件。

## 历史外部持久化（价格对比的权威源放 GitHub）

> 背景：`data/history.json` 是「较昨日/较上周/较上月」对比所依赖的累积采样。线上 `server.py`
> 部署在**单 HTTP 端口、本地磁盘可写但不可靠**的环境——实例冷启动 / 重新部署可能把本地
> `data/history.json` 回滚到发布时刻的旧快照，使历史无法跨生命周期累积。为此把**权威副本**迁到
> 一个 GitHub 私有仓库文件，本地 `data/history.json` 降级为缓存副本。

**架构**：`fetch-prices.py` 每次刷新时，先从 GitHub 拉取最新历史（权威）作为累积基线 → 追加本次
样本 → 乐观锁（sha）写回 GitHub；随后同步一份到本地 `data/history.json`（供前端读取 / 离线 / 回滚）。
`scripts/gh-history.py` 封装全部 GitHub Contents API 读写，仅用 Python 标准库。

**配置**（均可选；缺失则自动回退为只写本地，与旧版等价）：

| 环境变量 | 说明 |
|---|---|
| `GH_HISTORY_TOKEN` | GitHub PAT，目标私有仓库 Contents 读写权限 |
| `GH_HISTORY_REPO` | `owner/repo`（私有仓库须已存在） |
| `GH_HISTORY_PATH` | 仓库内文件路径，默认 `history.json` |

示例见 `.env.example`。**不要把 token 硬编码进任何源码**；通过环境变量在运行/发布时注入。

```bash
# 本机验证 GitHub 后端可用后，跑一次抓取（自动走 GitHub 读写）
set GH_HISTORY_TOKEN=ghp_xxx&& set GH_HISTORY_REPO=you/your-repo&& python scripts/fetch-prices.py

# 线上 server.py：把以上环境变量注入线上进程即可，代码无需改
```

**并发安全**：多处实例（如本机计划任务 + 线上 server）可能同时刷新。写回用 GitHub 文件 sha 做乐观锁，
冲突(409)时自动重拉最新副本并合并去重后重试，不会互相覆盖、不会丢样本。

**降级**：GitHub 不可达 / 未配 token 时，刷新照常进行并只写本地；GitHub 恢复后以远程为基线再合并，
不丢失已累积样本。抓取失败永远不会让价格页面报错。

## 配置改动

- **增删模型**：编辑 `data/models.json` 的 `models` 列表
- **换变体**：改模型的 `or_id`（OpenRouter ID），或把备选加进 `fallbacks` 数组
- **换厂商**：改 `vendors` 中的 URL 即可，按钮跳转会自动跟随
- **改新闻源 / 关键词 / 分类**：编辑 `scripts/fetch-news.py` 顶部的 `SOURCES` / `AI_KEYWORDS` / `AI_BLOCK` / `CATEGORY_RULES`
- **新闻回溯窗口**：`python scripts/fetch-news.py --days=3`（默认 2 天，覆盖前一日+当日早上）
- **改频率**：用 `Set-ScheduledTask` 重新设置 `RepetitionInterval`（价格）或改 `-Daily -At`（新闻）