# YourTurn

**Run many Claude Code sessions. Know when it's your turn.**

A local Kanban board for running many [Claude Code](https://docs.claude.com/en/docs/claude-code) sessions at once.
Your Linear / Jira issues and local tasks become cards; each card can start a Claude Code session in tmux, and the
board shows which sessions are working, which are **waiting for you**, and lets you answer them from an embedded
terminal in the browser (or your phone).

*[Leia em português](README.pt-BR.md)*

- **Lanes**: To do · Claude working · Waiting for you · In review · Done (last 7 days). A card moves by itself from the session state
  (Claude Code hooks + what is on the tmux screen); you can also move it by hand.
- **Sessions per card**: start Claude Code in a repo (optionally in a git worktree per card), resume, hibernate idle
  sessions, send the same message to several sessions, reminders that reopen terminals still waiting for you.
- **Embedded terminal** (xterm.js over a WebSocket to the tmux window), file upload, maximize/tile.
- **Tools**: Linear and/or Jira (Cloud or Data Center), GitHub or Bitbucket (PR badges: checks, reviews, comments),
  Slack (DMs and mentions in a side panel, reply from the board), per-card summaries with Claude Haiku.
- **Organizations**: keep several accounts apart (e.g. two employers and personal): each one has its own
  `CLAUDE_CONFIG_DIR`, GitHub login, commit e-mail, Linear/Jira/Slack credentials and project folders.
- **Phone**: installable PWA with Web Push notifications ("session X is waiting for you"), through Tailscale.
- **Resources page** (`/recursos`): CPU, memory, disks, Docker, who is consuming what (grouped by Claude session),
  guarded kill/stop actions, disk cleanup (old worktrees, caches). On WSL it also shows the Windows side.
- **Languages**: Português (Brasil) and English (US), in Settings.

Zero dependencies: Python standard library + one HTML file. No build step.

## Requirements

- Linux or Windows with WSL2 (macOS is untested: the resources page reads `/proc`).
- Python 3.10+, `tmux`, `git`, and the `claude` CLI logged in.
- Optional: `gh` (GitHub PRs), `docker`, `python3-cryptography` (push notifications).

## Install

```bash
git clone https://github.com/FabioCruzDev/yourturn.git ~/yourturn
cd ~/yourturn
python3 tools/install_hooks.py   # adds the board's hooks to ~/.claude/settings.json (idempotent, keeps a backup)
./start.sh                       # starts the board inside tmux; open http://localhost:8765
```

`tools/install_hooks.py --remove` takes the hooks out. Sessions that were already open only pick the hooks up after a
restart. To start the board when you log in, call `start.sh` from your session startup (systemd user unit, `.profile`,
or on Windows a shortcut to `wsl.exe -- bash -lc "~/yourturn/start.sh"`).

Configuration is optional: copy `local.env.example` to `local.env` (not versioned) and adjust ports, repo folder, etc.

**Where sessions start:** a card's session opens in one of your git repositories: the folders directly under
`KANBAN_REPOS_ROOT` (default `~/Projects`, e.g. `~/Projects/my-app/.git`) plus the project folders of each organization.
Clone your repos there, or point `KANBAN_REPOS_ROOT` at the folder that holds them.

## Connect your tools

The first time you open the board, a short setup wizard asks for:

1. **Projects:** the folder with your repositories (it shows how many it found) and a name for the workspace.
2. **Tasks:** Linear, Jira (Cloud or Data Center) or board-only tasks. Paste the key and click **Test** to see how many
   issues are assigned to you before saving.
3. **Code:** GitHub (it detects whether `gh` is logged in and offers a login button) or Bitbucket.
4. **Extras:** commit e-mail and Slack token (optional), plus a summary of what will change on your machine.

If Claude Code is already logged in on the machine, the workspace reuses that login (`~/.claude` and `gh`), so nothing
new to sign in. You can skip the wizard (“Set up later”) and reopen it from the banner.

**More than one account** (e.g. two employers and personal)? Add another organization from the selector →
“+ New organization…”. Each extra organization gets its own `CLAUDE_CONFIG_DIR` (`~/.claude-<id>`, “Log in to Claude”
opens a terminal for it), GitHub login, commit e-mail and Linear/Jira/Slack credentials. Sessions started under an
organization's folders use its accounts.

Credentials are stored in `~/.config/claude-orgs/<id>/` with mode 600 and never leave your machine.

> **What setting up changes outside the board:** a managed block in `~/.bashrc` so that `claude` and `gh` pick the
> organization by folder, `includeIf "gitdir:<folder>/"` entries in `~/.gitconfig` when you set a commit e-mail, and,
> for extra organizations, `~/.claude-<id>` (generic settings, hooks, commands and plugins are linked from `~/.claude`).
> `tools/claude_orgs.py` (`verify`, `sync`, `add`, `env`) manages the same files from the command line.

## Security

The board **types into your terminals**. Treat access to it as shell access.

- The server only listens on `127.0.0.1` and checks `Host` and `Origin` (DNS-rebinding and CSRF protection).
- For remote access use a private network, e.g. `tailscale serve --bg 8765` and `KANBAN_HOSTS=<pc>.<tailnet>.ts.net`
  in `local.env`. **Never** bind to `0.0.0.0`, use `tailscale funnel`, or put it behind a public tunnel.
- Tokens are read from files under `~/.config` with mode 600; the board never sends them anywhere but the tool's API.

## Optional extras

- **Saver mode / “Drop cache”** (resources page) can drop the Linux page cache; this needs a narrow sudo rule:
  `sudo bash tools/install-eco-sudo.sh` (allows only the two `sysctl` commands it uses).
- `KANBAN_DISKS="Data=/mnt/data"` shows extra disks on the resources page.
- Short PR references like `api#12` can be resolved for one GitHub org: `KANBAN_GH_ORG`, `KANBAN_PR_SHORT`.
- `KANBAN_VOICE=1` enables “Jarvis”, an experimental voice assistant (browser speech recognition + `claude -p`).
  Off by default and not supported.

## Development

```bash
python3 test_server.py && python3 test_resources.py && python3 tools/test_bb_pr.py
```

Tests never touch your real data when run with a throwaway `HOME` and `KANBAN_TMUX=kanban-test`. UI texts are written in
Portuguese and translated through `i18n_en.json` (`t("...")` in the pages, `tr("...")` in the server): when you add a
visible string, add its English entry.

## License

MIT, see [LICENSE](LICENSE). Bundled xterm.js: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
