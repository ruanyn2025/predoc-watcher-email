# predoc-watcher-email

[English](README.md) · **中文**

每日邮件推送主流经济学 pre-doc / RA 招聘页的新岗位。

| 来源 | 页面 |
|---|---|
| predoc.org | [/opportunities](https://www.predoc.org/opportunities) |
| NBER（本部） | [research-assistant-positions-nber](https://www.nber.org/career-resources/research-assistant-positions-nber) |
| NBER（非本部） | [research-assistant-positions-not-nber](https://www.nber.org/career-resources/research-assistant-positions-not-nber) |

> 想要带界面的版本（搜索、收藏、日历提醒、申请追踪），见
> [predoc_watcher_app](https://github.com/ruanyn2025/predoc_watcher_app)，
> 也可以先[在浏览器里试用](https://claude.ai/code/artifact/4c78e2a9-6060-4889-bd3e-9f78ede18096)。两者互相独立，可只用其一。

## 安装

需要 Python 3.9 或更高版本。

```bash
git clone https://github.com/ruanyn2025/predoc-watcher-email.git
cd predoc-watcher-email
pip install -r requirements.txt
```

## 配置邮箱

复制一份配置模板：

```bash
cp config.example.json config.json
```

Gmail 的 SMTP 不接受账号登录密码，需要单独生成一个应用专用密码：

1. 在 Google 账号里开启两步验证。未开启时看不到下一步的入口。
2. 打开 <https://myaccount.google.com/apppasswords>，生成一个，复制那 16 位字母。
3. 填进 `config.json`：`app_password` 填这 16 位，`user` 和 `to` 填你的邮箱地址。

`config.json` 含密码，已列入 `.gitignore`，不要提交或分享。

其余可选项：

| 配置项 | 作用 |
|---|---|
| `sources` | 三个来源各自的开关 |
| `daily_email_even_if_empty` | 默认 `true`，没有新岗位时也发一封简短邮件，用来确认程序仍在运行。设为 `false` 则只在有新增时发信。 |
| `verification_email_full_list` | 默认 `true`，首封验证信附上当前全部在招岗位清单。设为 `false` 则只报数量。 |

## 首次运行

```bash
python watch_jobs.py
```

配置好密码后的第一次运行会发出一封验证信，同时把当前所有在招岗位记为基线，此后每日仅通知新增岗位。收到这封信即表示配置成功。

## 设为定时任务

**Windows**

```powershell
.\setup_task.ps1
```

注册一个每天 09:07 运行的计划任务，运行时不弹出窗口。

```powershell
.\setup_task.ps1 -RunAt "07:23"                                  # 换时间
.\setup_task.ps1 -Python "C:\path\to\env\pythonw.exe"            # 指定解释器
Unregister-ScheduledTask -TaskName PredocWatcher -Confirm:$false # 卸载
```

到点时电脑关机或休眠会错过，开机后任务会自动补跑一次。任务在你登录后运行，因此不需要把 Windows 密码存进任务计划。

**macOS**

```bash
./setup_launchd.sh
```

注册一个 launchd 任务，每天 09:07 运行。

```bash
./setup_launchd.sh --at 07:23                    # 换时间
./setup_launchd.sh --python /opt/homebrew/bin/python3   # 指定解释器
./setup_launchd.sh --uninstall                   # 卸载
```

到点时机器睡着或关机，launchd 会在唤醒或开机后补跑一次。

> 这个脚本尚未在 macOS 真机上运行过。生成的配置文件经过校验，但如果遇到问题，
> 欢迎在仓库里提 issue。

**Linux** 用 cron：

```cron
7 9 * * * cd /path/to/predoc-watcher-email && /usr/bin/python3 watch_jobs.py
```

注意 cron **不会补跑**：到点时机器不在运行状态，这一天就直接跳过了。笔记本用户可以改用
systemd timer 的 `Persistent=true`，它有补跑行为。

## 命令

| 命令 | 作用 |
|---|---|
| `python watch_jobs.py` | 检查一次，有新增则发信 |
| `python watch_jobs.py --test-email` | 只发一封测试邮件，检查邮箱配置 |
| `python watch_jobs.py --dry-run` | 只抓取并打印结果，不发信、不改数据 |
| `python watch_jobs.py --source predoc` | 只检查指定来源，可重复指定 |
| `python watch_jobs.py --resend-verification` | 下次运行时重发验证信 |
| `python watch_jobs.py --rebaseline` | 把当前所有岗位重设为基线，不发信 |

## 你会收到三种邮件

- **验证信**：邮箱首次配置成功时发一封，附当前在招岗位清单。
- **有新岗位**：按来源分组，每条列出标题、机构、导师、研究领域、截止日期和申请链接。
- **无新岗位**：一行说明加各来源的在招数量。

## 排错

**没收到邮件。** 先检查垃圾邮件箱，再运行 `--test-email` 单独测试邮箱配置。

出现 `Username and Password not accepted`，通常是填了 Gmail 登录密码而不是应用专用密码，或者没有开启两步验证。

还要确认定时任务确实运行过。Windows：

```powershell
Get-ScheduledTaskInfo -TaskName PredocWatcher
```

`LastTaskResult` 应为 `0`。macOS：

```bash
launchctl print gui/$UID/local.predoc-watcher-email
```

看 `last exit code`，应为 `0`。想立刻跑一次：

```bash
launchctl kickstart -k gui/$UID/local.predoc-watcher-email
```

**邮件里出现抓取失败的提示。** 说明某个来源本次没抓成功。偶尔一两次通常是网络问题，上次的数据会保留，不会丢失或误报。如果连续多天出现，多半是网站改版了，需要更新解析代码。

程序在开工前会先等域名能解析（最多 5 分钟），因为定时任务常常在电脑刚唤醒、网络还没就绪时就跑起来了。抓取和发信各自也会重试。如果日志里写着「等待网络 300 秒仍无法解析域名」，那就是运行的时候确实没网。

**查看日志：**

```powershell
Get-Content logs\watch.log -Tail 30      # Windows
```

```bash
tail -30 logs/watch.log                  # macOS / Linux
```

## 文件

```
watch_jobs.py         主程序
config.example.json   配置模板
config.json           你的配置（含密码，请不要上传至公开渠道）
state.json            已知岗位记录，请勿手动修改
logs/watch.log        运行日志
setup_task.ps1        Windows 计划任务安装脚本
setup_launchd.sh      macOS launchd 安装脚本
extra_ca/gdig2.pem    predoc.org 缺失的一张 CA 中间证书，程序运行时需要
.ca_bundle.pem        运行时自动生成，可以删除
```

## 限制

- 电脑关机期间不会检查。开机后补跑时只能看到当下的差异，关机期间上线又下架的岗位会漏掉。
- 两个 NBER 页面不提供截止日期，只有 predoc.org 的岗位带这一项。
- 只通知新增岗位，岗位下架或信息修改不会通知。

## 许可证

MIT，见 [LICENSE](LICENSE)。
