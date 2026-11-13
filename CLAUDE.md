# YourTurn

Local Kanban board for Claude Code sessions. Read `README.md` first.

## Rules

- `server.py` (stdlib only, port 8765, runs in tmux `kanban:quadro`) and a single `index.html` (F5 applies). Restarting
  the server closes the terminals open in the browser.
- The board types into terminals: never bind `0.0.0.0` nor expose it publicly (only `tailscale serve` + `KANBAN_HOSTS`).
- Tests are isolated: copy to a scratch dir, `KANBAN_TMUX=kanban-test`, a fake `launched.json`, `HOME=<scratch>/home` when
  you need fake session files. Never use the real `launched.json` nor write fake jsonl into the real history.
- `python3 test_server.py`, `python3 test_resources.py`, `python3 tools/test_bb_pr.py` (no pytest).
- Every `claude` the board starts gets its organization explicitly (`claude_env` / `claude_cmd`), never inherited.
- Visible text is written in pt-BR and translated in `i18n_en.json`: `t()` in the pages, `tr()` in the server.
- Don't use `pkill -f` with a pattern that appears in your own command line (it kills your shell).
