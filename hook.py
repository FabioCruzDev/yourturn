#!/usr/bin/env python3
"""Hook do Claude Code: grava se a sessão está trabalhando ou esperando você, para o quadro ler.

Configurado em ~/.claude/settings.json; recebe o JSON do evento no stdin. Nunca falha nem imprime nada:
um hook quebrado não pode atrapalhar a sessão.
"""
import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path("~/.cache/kanban-claude/state").expanduser()
ASKS_USER = {"AskUserQuestion", "ExitPlanMode"}  # ferramentas que são pergunta para você


def state_for(ev):
    name = ev.get("hook_event_name", "")
    if name == "PreToolUse":
        return "waiting" if ev.get("tool_name") in ASKS_USER else "working"
    return {
        "UserPromptSubmit": "working",
        "PostToolUse": "working",
        "PostToolUseFailure": "working",
        "PermissionRequest": "waiting",
        "Notification": "waiting",
        "Stop": "waiting",
        "StopFailure": "waiting",
        "SessionEnd": "ended",
    }.get(name)


def main():
    try:
        ev = json.load(sys.stdin)
        sid, state = ev.get("session_id"), state_for(ev)
        if not sid or not state or "/" in sid:
            return
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / f".{sid}.{os.getpid()}"
        tmp.write_text(json.dumps({"state": state, "event": ev.get("hook_event_name"), "ts": time.time()}))
        tmp.replace(STATE_DIR / f"{sid}.json")  # atômico: o quadro nunca lê arquivo pela metade
    except Exception:
        pass


if __name__ == "__main__":
    main()
