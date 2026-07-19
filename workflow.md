# Daily Planner Workflow

## 1. 项目身份对应

- 本地项目目录：`D:\personal\ai_projects\60.daily_schedule`
- 本地关键文件：`app/telegram_bot.py`
- GitHub 仓库名：`qizhangumich/daily-planner`
- GitHub 仓库地址：`https://github.com/qizhangumich/daily-planner`
- Hetzner 服务器：`root@157.180.115.88`（Helsinki，CX23，2026-07-19 从旧服务器 195.201.31.11 迁移）
- Hetzner 部署目录：`/opt/daily-planner`
- Hetzner 运行分支：`main`
- Docker Compose 项目名：`daily-planner`
- Docker 服务名：`telegram-notion-daily-assistant`
- Docker 容器名：`telegram-notion-daily-assistant`
- Docker 镜像名：`daily-planner-telegram-notion-daily-assistant`

## 2. 调用关系

整体链路如下：

`本地代码` -> `GitHub 仓库` -> `Hetzner /opt/daily-planner` -> `docker compose build/run` -> `Telegram Bot`

更具体一点：

1. 你在本地修改代码。
2. 代码推送到 GitHub 仓库 `daily-planner`。
3. Hetzner 上的 `/opt/daily-planner` 同步最新代码。
4. 在 Hetzner 上执行 `docker compose up -d --build`。
5. 容器 `telegram-notion-daily-assistant` 重建后，Telegram 中的 bot 才会更新。

## 3. 当前部署事实

- 服务器实际运行目录是：`/opt/daily-planner`
- 运行中的容器名是：`telegram-notion-daily-assistant`
- 服务器上的运行数据在：`/opt/daily-planner/data`
- 其中 `data/app.db` 和 `data/app.log` 是运行产物，通常不应该作为代码改动提交

## 4. 自动部署

仓库里现在已经增加了 GitHub Actions 自动部署：

- workflow 文件：`.github/workflows/deploy.yml`
- workflow 名称：`Deploy to Hetzner`
- 触发条件：
  `push` 到 `main`
  或在 GitHub Actions 页面手动点击 `Run workflow`

自动部署链路：

`本地 push 到 GitHub main` -> `GitHub Actions` -> `SSH 登录 Hetzner` -> `git pull` -> `docker compose up -d --build`

### 需要配置的 GitHub Secrets

在 GitHub 仓库 `Settings` -> `Secrets and variables` -> `Actions` 中添加：

- `HETZNER_HOST`
  值：`157.180.115.88`
- `HETZNER_USER`
  值：`root`
- `HETZNER_SSH_KEY`
  值：用于登录 Hetzner 的私钥全文

### 推荐的 key 方案

建议专门创建一把只用于 GitHub Actions 自动部署的 SSH key，不要直接复用你自己电脑的长期私钥。

推荐步骤：

1. 在本地生成新的部署 key
2. 把公钥追加到 Hetzner 的 `/root/.ssh/authorized_keys`
3. 把私钥内容保存到 GitHub Secret `HETZNER_SSH_KEY`

### 自动部署执行的服务器命令

```bash
cd /opt/daily-planner
git remote set-url origin git@github.com:qizhangumich/daily-planner.git
git fetch origin main
git pull --rebase --autostash origin main
docker compose up -d --build
docker ps --filter name=telegram-notion-daily-assistant
```

说明：

- 这里使用 SSH 仓库地址，避免服务器因为 GitHub HTTPS 凭证缺失而拉取失败
- 这里没有使用 `git reset --hard`，避免误伤服务器上的运行数据
- `git pull --rebase --autostash` 更适合当前这个仓库，因为服务器上的 `data/app.db` 与 `data/app.log` 会持续变化

## 5. 常用更新流程

### 方案 A：标准流程

这是以后最推荐的方式。

1. 在本地修改文件
2. 在本地提交并推送到 GitHub
3. GitHub Actions 自动部署到 Hetzner
4. 如果需要，再去服务器看日志确认结果

### 方案 B：服务器直接改

适合紧急修复，但不建议长期这样做。

1. 直接在 Hetzner 的 `/opt/daily-planner` 修改代码
2. 在服务器仓库里 `git add` / `git commit` / `git push`
3. 执行 `docker compose up -d --build`

风险：

- 本地代码可能和服务器代码不同步
- 容易忘记把修复同步回本地

## 6. 本次改动对应位置

这次“记录 / 回顾 / 反思”标签入口改的是：

- 本地文件：`D:\personal\ai_projects\60.daily_schedule\app\telegram_bot.py`
- GitHub 文件：`app/telegram_bot.py`
- Hetzner 文件：`/opt/daily-planner/app/telegram_bot.py`

## 7. 常用命令

### 本地查看 git 状态

```powershell
git status --short
```

### 本地推送到 GitHub

```powershell
git add app/telegram_bot.py
git commit -m "your message"
git push origin main
```

### 登录 Hetzner

```powershell
ssh root@157.180.115.88
```

### Hetzner 手动拉最新代码并重建

```bash
cd /opt/daily-planner
git pull origin main
docker compose up -d --build
```

### 查看容器状态

```bash
docker ps --filter name=telegram-notion-daily-assistant
```

### 查看最近日志

```bash
docker logs --tail 50 telegram-notion-daily-assistant
```

### 手动触发自动部署

在 GitHub 仓库 `Actions` 页面中，找到 `Deploy to Hetzner`，点击 `Run workflow`。

## 8. 如果以后忘了名字，看这几项

- 本地目录名：`60.daily_schedule`
- GitHub 仓库名：`daily-planner`
- 服务器目录名：`/opt/daily-planner`
- 容器名：`telegram-notion-daily-assistant`
- 自动部署 workflow：`.github/workflows/deploy.yml`

## 9. 建议

以后只要你说“帮我更新 daily planner”，建议默认按下面这个顺序理解：

1. 改本地项目 `D:\personal\ai_projects\60.daily_schedule`
2. 推送到 GitHub 仓库 `qizhangumich/daily-planner`
3. 让 GitHub Actions 自动同步到 Hetzner `/opt/daily-planner`
4. 重建容器 `telegram-notion-daily-assistant`

这样整个链路最清楚，也最不容易漏步骤。
