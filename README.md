# predoc-watcher-email

**English** · [中文](README.zh-CN.md)

Emails you the new postings from the main econ pre-doc / RA job boards, once a day.

| Source | Page |
|---|---|
| predoc.org | [/opportunities](https://www.predoc.org/opportunities) |
| NBER (internal) | [research-assistant-positions-nber](https://www.nber.org/career-resources/research-assistant-positions-nber) |
| NBER (external) | [research-assistant-positions-not-nber](https://www.nber.org/career-resources/research-assistant-positions-not-nber) |

> For a version with an interface — search, starring, calendar reminders, application
> tracking — see [predoc_watcher_app](https://github.com/ruanyn2025/predoc_watcher_app),
> or [try it in your browser](https://claude.ai/code/artifact/4c78e2a9-6060-4889-bd3e-9f78ede18096) first.
> The two are independent; you can use either on its own.

## Install

Requires Python 3.9 or later.

```bash
git clone https://github.com/ruanyn2025/predoc-watcher-email.git
cd predoc-watcher-email
pip install -r requirements.txt
```

## Set up your mailbox

Copy the config template:

```bash
cp config.example.json config.json
```

Gmail's SMTP does not accept your account password. Generate an app password instead:

1. Turn on 2-Step Verification in your Google account. The next step is not available without it.
2. Open <https://myaccount.google.com/apppasswords>, create one, and copy the 16 letters.
3. Put them in `config.json` as `app_password`, and set `user` and `to` to your email address.

`config.json` holds that password. It is listed in `.gitignore` — do not commit or share it.

Other settings:

| Setting | What it does |
|---|---|
| `sources` | Turns each of the three sources on or off |
| `daily_email_even_if_empty` | `true` by default: sends a short email even when nothing is new, so you can tell the program is still running. Set to `false` to only hear from it when there are new postings. |
| `verification_email_full_list` | `true` by default: the first email lists every posting currently open. Set to `false` for counts only. |

## First run

```bash
python watch_jobs.py
```

The first run after you set the password sends a confirmation email and records everything
currently open as the baseline. From then on the daily email covers only new postings.
Getting that email means the setup worked.

## Run it on a schedule

**Windows**

```powershell
.\setup_task.ps1
```

Registers a scheduled task that runs daily at 09:07 with no window popping up.

```powershell
.\setup_task.ps1 -RunAt "07:23"                                  # different time
.\setup_task.ps1 -Python "C:\path\to\env\pythonw.exe"            # choose an interpreter
Unregister-ScheduledTask -TaskName PredocWatcher -Confirm:$false # remove
```

If the machine is off or asleep at that time, the task runs once after the next boot instead.
It runs while you are logged in, so your Windows password is never stored in Task Scheduler.

**macOS**

```bash
./setup_launchd.sh
```

Registers a launchd agent that runs daily at 09:07.

```bash
./setup_launchd.sh --at 07:23                           # different time
./setup_launchd.sh --python /opt/homebrew/bin/python3   # choose an interpreter
./setup_launchd.sh --uninstall                          # remove
```

If the machine is asleep or off at that time, launchd runs the job once after it wakes or boots.

> This script has not yet been run on a real Mac. The plist it generates is validated, but if you
> hit a problem, please open an issue.

**Linux** — use cron:

```cron
7 9 * * * cd /path/to/predoc-watcher-email && /usr/bin/python3 watch_jobs.py
```

Note that cron does **not** catch up: if the machine is not running at that time, the day is simply
skipped. On a laptop, a systemd timer with `Persistent=true` does catch up.

## Commands

| Command | What it does |
|---|---|
| `python watch_jobs.py` | Check once, email if anything is new |
| `python watch_jobs.py --test-email` | Send a test email to check the mailbox settings |
| `python watch_jobs.py --dry-run` | Fetch and print only — no email, no changes saved |
| `python watch_jobs.py --source predoc` | Check one source; can be repeated |
| `python watch_jobs.py --resend-verification` | Send the confirmation email again on the next run |
| `python watch_jobs.py --rebaseline` | Reset the baseline to everything currently open, without emailing |

## The three kinds of email

- **Confirmation** — sent once when the mailbox is first set up, listing what is currently open.
- **New postings** — grouped by source, each with title, institution, researchers, fields,
  deadline and a link to apply.
- **Nothing new** — one line plus the number of open postings per source.

## Troubleshooting

**No email arrived.** Check the spam folder, then run `--test-email` to test the mailbox on its own.

`Username and Password not accepted` usually means a Gmail account password was used instead of
an app password, or 2-Step Verification is off.

Check that the scheduled job actually ran. On Windows:

```powershell
Get-ScheduledTaskInfo -TaskName PredocWatcher
```

`LastTaskResult` should be `0`. On macOS:

```bash
launchctl print gui/$UID/local.predoc-watcher-email
```

Look at `last exit code`; it should be `0`. To run it right now:

```bash
launchctl kickstart -k gui/$UID/local.predoc-watcher-email
```

**The email says a source failed.** One of the boards could not be read this time. An occasional
failure is usually a network problem; the previous data is kept, so nothing is lost or wrongly
reported. If it happens for several days running, the site has probably changed and the parsing
code needs updating.

**Read the log:**

```powershell
Get-Content logs\watch.log -Tail 30      # Windows
```

```bash
tail -30 logs/watch.log                  # macOS / Linux
```

## Files

```
watch_jobs.py         the program
config.example.json   config template
config.json           your config (holds the password; do not upload it anywhere public)
state.json            record of known postings; do not edit by hand
logs/watch.log        run log
setup_task.ps1        Windows scheduled task installer
setup_launchd.sh      macOS launchd installer
extra_ca/gdig2.pem    a CA certificate predoc.org omits; the program needs it to connect
.ca_bundle.pem        generated at runtime; safe to delete
```

## Limits

- Nothing is checked while the machine is off. The catch-up run only sees the current state, so a
  posting that appeared and closed in the meantime is missed.
- The two NBER pages do not publish deadlines; only predoc.org postings have one.
- You are told about new postings only — not about postings being removed or edited.

## License

MIT, see [LICENSE](LICENSE).
