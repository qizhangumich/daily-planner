# telegram-notion-daily-assistant

一个由 Telegram 驱动、Notion 作为数据库、OpenAI 作为理解引擎的 Daily Execution Assistant。你可以随时在 Telegram 里输入文字或语音，系统会自动整理任务、写入当天的 Notion Daily Record，并在晚上固定时间主动提醒你完成复盘与反思。

## 功能概览

- 支持 Telegram 文本任务录入
- 支持 Telegram 语音转文字后录入任务
- 每天自动写入或更新 Notion Daily Record
- 晚间自动提醒任务复盘
- 支持通过文字或语音回复复盘
- 支持写入今日反思
- 本地使用 SQLite 保存状态和缓存
- 支持 Docker 与 docker-compose 长期运行
- 仅允许 `TELEGRAM_USER_ID` 指定用户访问

## 项目结构

```text
telegram-notion-daily-assistant/
  app/
    main.py
    config.py
    telegram_bot.py
    notion_client.py
    openai_client.py
    speech_client.py
    scheduler.py
    daily_record_service.py
    task_parser.py
    review_parser.py
    reflection_parser.py
    state_manager.py
    storage.py
    logger.py
  prompts/
    parse_task.md
    parse_review.md
    parse_reflection.md
  data/
    app.db
    app.log
  Dockerfile
  docker-compose.yml
  requirements.txt
  .env.example
  README.md
```

## 1. 如何创建 Telegram Bot

1. 在 Telegram 搜索 `@BotFather`。
2. 发送 `/newbot`。
3. 按提示输入 Bot 名称。
4. 再输入一个以 `bot` 结尾的唯一用户名。
5. 创建成功后，BotFather 会返回一个 Bot Token。

## 2. 如何获取 Telegram Bot Token

BotFather 创建完成后会直接返回类似下面的字符串：

```text
1234567890:ABCDEF_your_bot_token_here
```

把它填入 `.env` 的 `TELEGRAM_BOT_TOKEN`。

## 3. 如何获取自己的 Telegram User ID

可用以下任一方式：

1. 在 Telegram 搜索 `@userinfobot`。
2. 给它发送任意消息。
3. 它会返回你的用户 ID。

把这个数字填入 `.env` 的 `TELEGRAM_USER_ID`。

## 4. 如何创建 Notion Integration

1. 打开 [Notion Integrations](https://www.notion.so/profile/integrations)。
2. 点击 `New integration`。
3. 给 integration 命名，例如 `telegram-notion-daily-assistant`。
4. 选择你的 workspace。
5. 创建后复制 `Internal Integration Token`。

把它填入 `.env` 的 `NOTION_TOKEN`。

## 5. 如何创建 Daily Records 数据库

1. 在 Notion 新建一个 full page database。
2. 命名为 `Daily Records`。
3. 创建以下字段：

- `Title`：Title
- `Date`：Date
- `Tasks`：Rich Text
- `Review Status`：Select
- `Completion Score`：Number
- `Daily Summary`：Rich Text
- `Carry Over Tasks`：Rich Text
- `Last Updated`：Date

`Review Status` 需要先创建以下选项：

- `Not Started`
- `Waiting Review`
- `Reviewed`

## 6. 如何把 Notion database share 给 integration

1. 打开 `Daily Records` 数据库页面。
2. 点击右上角 `Share`。
3. 选择刚刚创建的 integration。
4. 确认授予访问权限。

## 7. 如何获取 Notion Database ID

打开数据库页面，URL 通常类似：

```text
https://www.notion.so/yourworkspace/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx?v=...
```

其中 `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` 就是数据库 ID。填入 `.env` 的 `NOTION_DAILY_DATABASE_ID`。

## 8. 如何配置 .env

复制模板：

```bash
cp .env.example .env
```

填写以下内容：

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_USER_ID=your_telegram_user_id

NOTION_TOKEN=your_notion_integration_token
NOTION_DAILY_DATABASE_ID=your_notion_database_id

OPENAI_API_KEY=your_openai_api_key

TIMEZONE=Asia/Singapore
DAILY_REVIEW_HOUR=22
DAILY_REVIEW_MINUTE=30

DATABASE_PATH=./data/app.db
LOG_PATH=./data/app.log
```

## 9. 如何本地运行

建议使用 Python 3.11。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

## 10. 如何用 Docker 运行

```bash
docker compose up -d --build
```

停止：

```bash
docker compose down
```

## 11. 如何部署到 VPS

1. 安装 Docker 与 Docker Compose。
2. 把项目上传到 VPS。
3. 在 VPS 上创建 `.env`。
4. 执行：

```bash
docker compose up -d --build
```

5. 用 `docker compose logs -f` 查看启动日志。
6. 建议结合 systemd 或直接依赖 `restart: always` 长期运行。

## 12. 如何测试文字任务

1. 给 Bot 发送 `/start`。
2. 直接发送一句文本，例如：

```text
今天要完成周报，下午跟进客户邮件，晚上看 30 分钟 Python 异步编程
```

3. Bot 应该返回新增任务清单。
4. 在 Notion 当天记录中应看到 `Tasks`、`Daily Summary`、`Completion Score` 等字段被更新。

## 13. 如何测试语音任务

1. 给 Bot 发送一段语音。
2. Bot 会先返回转写文本。
3. 随后把转写结果当作任务解析并写入 Notion。

建议第一次测试时，说话清晰且任务表达直接，例如：

```text
今天下午三点前把项目计划改完，晚上回复投资人邮件
```

## 14. 如何测试晚上复盘提醒

有两种方式：

1. 直接发送 `/review`，立即手动触发复盘流程。
2. 临时把 `.env` 中的 `DAILY_REVIEW_HOUR` 和 `DAILY_REVIEW_MINUTE` 改成接下来几分钟，重启服务后等待触发。

收到提醒后，直接回复：

```text
周报完成了，客户邮件只回了一半，Python 学习没开始，因为晚上太累了
```

然后 Bot 会继续要求你补充今日反思。

## 15. 如何测试今日反思

在复盘之后直接回复，例如：

```text
今天节奏前面不错，但晚上精力掉得很快。我发现如果下午不先处理最重要的任务，晚一点就容易拖延。
```

Bot 应该返回“今天的记录已经完成”，并把 Reflection 写入 Notion。

## 16. 如何查看日志

本地日志文件默认在：

- [data/app.log](D:\personal\ai_projects\60.daily_schedule\data\app.log)

如果使用 Docker：

```bash
docker compose logs -f
```

## 17. 常见错误排查

`Missing required environment variable`

- 说明 `.env` 没填完整，检查所有必填项。

`Sorry, you are not authorized to use this bot.`

- 说明当前 Telegram 用户 ID 与 `TELEGRAM_USER_ID` 不一致。

Notion 写入失败

- 检查数据库字段名称是否与 README 完全一致。
- 检查数据库是否已 share 给 integration。
- 检查 `NOTION_DAILY_DATABASE_ID` 是否正确。

Telegram 收不到消息

- 检查 `TELEGRAM_BOT_TOKEN` 是否正确。
- 确认 bot 已经在 Telegram 中被你主动发起过会话。

语音转写失败

- 检查 `OPENAI_API_KEY` 是否有效。
- 检查 OpenAI 账户是否可调用语音转写模型。
- 检查网络是否可访问 OpenAI API。

定时任务不触发

- 检查 `TIMEZONE` 是否正确。
- 检查容器时间与系统时间是否一致。
- 临时把提醒时间调到几分钟后重新验证。

## 18. 第一版工作流说明

1. 白天直接发送文本或语音，默认都会被识别成新增任务。
2. 晚上到达设定时间后，Bot 会主动提醒复盘，并把状态切换为 `awaiting_review`。
3. 你回复复盘内容后，系统会生成完成度与延续任务，并切换到 `awaiting_reflection`。
4. 你回复反思内容后，系统会整理今日反思并把状态恢复为 `idle`。

## 19. 注意事项

- 第一版使用 Telegram polling，不使用 webhook。
- 当前只支持一个授权用户。
- `Tasks` 字段现在保存人类可读的任务清单，不再直接显示 JSON。
- `Daily Summary` 会汇总当天输入、复盘内容和反思内容，避免 Notion 字段过多。
- SQLite 主要用于本地状态与缓存，Notion 负责保存最终 Daily Record。
