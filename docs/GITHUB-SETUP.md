# GitHub 托管开通指南（三步，约 5 分钟）

> 目标：把 AI 价格看板迁到 GitHub Pages——链接永久稳定、数据存仓库、每天自动抓取发布，
> 彻底摆脱沙盒回收/链接失效问题。以下每一步只需浏览器点选，无需命令行。

## 第 1 步：建仓库（公开）

1. 打开 https://github.com/new （登录你的 GitHub 账号）
2. 填写：
   - **Repository name**：`ai-price-dashboard`（建议就用这个名字）
   - **可见性**：选 **Public**（免费账号开 Pages 必须 Public；价格数据无敏感性）
   - **不要**勾选 "Add a README"（保持空仓库，方便首次推送）
3. 点击绿色按钮 **Create repository**

## 第 2 步：生成 fine-grained PAT（个人访问令牌）

1. 打开 https://github.com/settings/personal-access-tokens/new
2. 填写：
   - **Token name**：`ai-price-dashboard-sync`
   - **Expiration**：建议 90 天或 1 年（到期前 GitHub 会发邮件提醒续期）
   - **Repository access**：选 **Only select repositories** → 勾选刚建的 `ai-price-dashboard`
   - **Permissions → Repository permissions**：
     - **Contents**：**Read and write**（读取与提交 data/ 数据）
     - 其他保持 No access 即可
3. 点击 **Generate token**，复制生成的 `github_pat_...`（**只显示一次**，存好）

> 这个 PAT 用于：①本机脚本与 GitHub 仓库双向同步（`gh-history.py` 已就绪）；
> ②由我把首版站点推送上仓库。若你不想发我，也可以自己 `git push`，流程在下方备注。

## 第 3 步：开启 Pages

1. 打开仓库页面 → **Settings** → 左侧 **Pages**
2. **Build and deployment → Source**：选 **Deploy from a branch**
3. **Branch**：选 `main`，目录选 `/ (root)` → **Save**
4. 一分钟后，仓库首页会显示部署地址：
   `https://<你的用户名>.github.io/ai-price-dashboard/` —— 这就是永久链接

## 把 PAT 交给助手（可选但推荐）

把 `仓库名` 和 `github_pat_...` 发到对话里，我会立即完成：
- 推送首版站点（含 `.github/workflows/update.yml`）
- 把本机抓取脚本切到 GitHub 数据源（历史数据不再怕本机/沙盒丢失）
- 核验 Pages 线上可用后，把新永久链接交给你，并停用旧的沙盒每日发布自动化

## 之后每天会发生什么（全自动）

- UTC 00:30（北京 08:30）：GitHub Actions 定时跑 `fetch-prices.py` + `fetch-news.py`
- 有数据变化 → 自动提交回仓库 → Pages 自动更新，**链接永远不变**
- 你也可以在仓库 **Actions** 页面手动点 "Run workflow" 立即更新
