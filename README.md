# MACD 邮件提醒 · 云端版（GitHub Actions）

把监控程序放在 GitHub 的服务器上跑，**每 30 分钟自动检查一次，电脑关机也照跑**。
不用买服务器、不用装任何软件，只要一个免费的 GitHub 账号。

## 监控内容

| 任务 | 币种 | 周期 | 触发信号 | 收件人 |
|---|---|---|---|---|
| BTC-4H | BTCUSDT | 4 小时 | 金叉、死叉、柱体变色 | wopanpanha@qq.com |
| BTC-15M | BTCUSDT | 15 分钟 | 金叉、死叉、柱体变色 | wopanpanha@163.com |
| ALT-4H-零轴下金叉 | AAVE、UNI、ARB、NEAR、ENA、EIGEN、ONDO | 4 小时 | 零轴下方金叉 | wopanpanha@163.com |

---

# 部署步骤（照着做，约 10 分钟）

## 第 1 步：注册 GitHub

打开 https://github.com/signup ，用邮箱注册并完成验证。已有账号直接跳到第 2 步。

## 第 2 步：创建仓库

1. 登录后点右上角 **+** → **New repository**
2. **Repository name** 填 `macd-alert`
3. 选 **Private**（私有，别人看不到）
4. 点绿色按钮 **Create repository**

## 第 3 步：上传程序文件

1. 在新仓库页面找到 **uploading an existing file** 这个链接（或点 **Add file** → **Upload files**）
2. 把本文件夹里的这 **4 个文件**拖进上传区：
   - `macd_alert.py`
   - `alert_config.json`
   - `alert_state.json`
   - `README.md`
3. 拉到底部点 **Commit changes**

> 注意：**先不要**拖 `.github` 文件夹（下一步单独建，成功率更高）。

## 第 4 步：新建工作流文件

1. 回到仓库首页，点 **Add file** → **Create new file**
2. 文件名框里输入（斜杠会自动建文件夹）：
   ```
   .github/workflows/macd-alert.yml
   ```
3. 把本文件夹里 `.github/workflows/macd-alert.yml` 的内容**全部复制粘贴**进去
4. 点 **Commit changes** → **Commit changes**

## 第 5 步：填入邮箱授权码（加密保存）

1. 仓库页面点 **Settings**（设置）
2. 左侧栏找到 **Secrets and variables** → **Actions**
3. 点 **New repository secret**
4. **Name** 填：`MACD_SMTP_PASSWORD`
5. **Secret** 填：你的 163 邮箱授权码（16 位那串）
6. 点 **Add secret**

> 这个 Secret 是加密的，仓库里任何人都看不到，也不会出现在代码里。

## 第 6 步：启动并验证

1. 仓库页面点 **Actions** 标签
2. 如果提示要启用，点 **I understand my workflows, go ahead and enable them**
3. 左侧点 **MACD 邮件提醒**
4. 右侧点 **Run workflow** → **Run workflow**
5. 等约 1 分钟刷新页面，看到绿色对勾 ✅ 表示成功
6. 检查你的 QQ 邮箱和 163 邮箱，应该能收到邮件

**跑通之后什么都不用管了** —— 它会每 30 分钟自动运行一次，永远不停（除非你手动关掉）。

---

# 常见问题

**Q：多久检查一次？能改吗？**
默认 30 分钟。想更快就把 `.github/workflows/macd-alert.yml` 里的 `cron: "*/30 * * * *"` 改成 `"*/15 * * * *"`（15 分钟）。

**Q：会不会不准时？**
GitHub 的定时任务在高峰期可能延迟几分钟到十几分钟，属于正常现象。但程序是**从上次已处理的 K 线逐根补扫**，所以信号**一个都不会漏**，只是到达时间可能有几分钟偏差。

**Q：免费吗？会超支吗？**
私有仓库每月免费 2000 分钟，每 30 分钟跑一次约消耗 1440 分钟，**在免费额度内**。改成 15 分钟一次则会超出。

**Q：GitHub 的服务器在海外，能抓到行情吗？**
能。程序内置 **6 个行情端点**（币安 4 个 + MEXC + Gate.io），按顺序自动尝试，**任一可用即可**。即使币安对海外机房 IP 返回限制，也会自动切换到 MEXC 或 Gate，不会中断。（这套容错对国内网络同样有效）

**Q：仓库 60 天没动静会被停用吗？**
普通定时任务会因为「仓库不活跃」被自动停用，但本程序每次运行都会自动提交一次游标更新，仓库一直有活动，**不会触发停用**。

**Q：怎么暂停或停止？**
仓库 **Actions** → 左侧 **MACD 邮件提醒** → 右上角 **⋯** → **Disable workflow**。

**Q：想改监控的币种或信号怎么办？**
在仓库里点开 `alert_config.json`，点右上角铅笔图标直接改，改完 **Commit changes**。可用的信号开关：`golden` 金叉、`death` 死叉、`goldenBelowZero` 零轴下方金叉、`deathAboveZero` 零轴上方死叉、`histFlip` 柱体变色、`zeroCross` 零轴穿越、`priceAbove`/`priceBelow` 价格突破（填数字，0 表示关闭）。

**Q：本地那套程序还要留着吗？**
**不要两个同时开**，否则同一个信号你会收到两封邮件。选一个即可：
- 用云端 → 在本地菜单里选 `5` 卸载计划任务
- 用本地 → 在仓库 Actions 里点 Disable workflow

---

*免责声明：本程序仅做行情指标监控与技术提示，不构成任何投资建议。加密资产波动剧烈，请自行控制风险。*
