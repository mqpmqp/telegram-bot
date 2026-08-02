# MingLi Telegram Console V1 部署与恢复

## 当前版本

- Hermes checkout：`/usr/local/lib/hermes-agent`
- 分支：`feature/hermes-telegram-mingli-console-v1`
- 当前 commit：以 `git rev-parse HEAD` 为准（本文件随该本地提交保存）
- 基线 HEAD（修改前）：`e598cef87465981fcea1c0339edfcf5d9716c917`
- MingLi 固定 SHA：`129ebd09df5c924cc4466e58271938f9b9a19875`

本文件随本地功能提交保存。提交后以 `git rev-parse HEAD` 作为当前 Hermes commit。

## 安装方式与加载路径

Hermes 是源码 checkout 的 editable install：

```text
/usr/local/lib/hermes-agent
/usr/local/lib/hermes-agent/venv
```

`pip show hermes-agent` 的 `Editable project location` 指向上述 checkout，因此当前分支代码会被 Python 直接加载。实际 Telegram Adapter 路径：

```text
/usr/local/lib/hermes-agent/plugins/platforms/telegram/adapter.py
```

systemd 实际启动：

```text
/usr/local/lib/hermes-agent/venv/bin/python -m hermes_cli.main gateway run
```

工作目录：`/root/.hermes`

## 环境变量

Gateway 的 Bot 环境变量由 Hermes 用户环境/配置加载。敏感值不得写入本文件、Git 或日志。

需要配置的 MingLi 变量：

- `TELEGRAM_ADMIN_IDS`：逗号分隔的 Telegram user ID
- `MINGLI_REPO`：默认 `/root/mingli-agent`
- `MINGLI_COMMIT_SHA`：默认固定 SHA
- `MINGLI_RUNTIME_TIMEOUT`：默认 `30`
- `MINGLI_CASES_DB`：默认 `~/.hermes/mingli/cases.sqlite3`

管理员 ID 必须通过 `/whoami` 从管理员本人消息中获得后写入受控环境，不得硬编码。

## 重启与检查

```bash
systemctl --user restart hermes-gateway.service
systemctl --user is-active hermes-gateway.service
systemctl --user show -p MainPID,ExecMainStartTimestamp --value hermes-gateway.service
```

如当前 Gateway 进程阻止从自身子进程重启，需从 Gateway 外部 shell 执行上述命令。

## 回滚

只允许回滚本地功能提交，不得触碰 MingLi checkout：

```bash
git -C /usr/local/lib/hermes-agent log --oneline --decorate -5
git -C /usr/local/lib/hermes-agent revert <local-commit>
systemctl --user restart hermes-gateway.service
```

## 升级覆盖风险

源码 checkout 的普通升级、重新安装或覆盖 `/usr/local/lib/hermes-agent` 可能覆盖本地 Console 修改。升级前必须保存本地提交；升级后按以下步骤重新应用：

1. 备份并保留本地功能提交。
2. 升级 Hermes 源码并重新执行 editable install。
3. 重新应用 MingLi Console 提交或人工解决 Adapter 冲突。
4. 重新运行 py_compile、unittest、MingLi 固定 SHA 冒烟。
5. 检查实际 Adapter import 路径和 systemd 主进程。
6. 重新执行管理员 `/whoami`、`/start` 冒烟。

## SQLite

案例库默认位于：

```text
~/.hermes/mingli/cases.sqlite3
```

案例 SQLite、WAL 文件和导出文件不得加入 Git。重新分析写入 `case_revisions`，不覆盖原始案例和 `created_at`。

## 禁止提交

- Telegram Bot Token
- GitHub Token
- 管理员 ID配置值
- Chat ID配置值
- 客户真实姓名
- 客户出生资料
- 完整分析正文导出
- 任何 Secret
