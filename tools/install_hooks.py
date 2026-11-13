#!/usr/bin/env python3
"""Instala (ou remove) os hooks do Claude Code que contam ao quadro se cada sessão está trabalhando ou esperando você.

  python3 tools/install_hooks.py                 instala no ~/.claude/settings.json (ou no $CLAUDE_CONFIG_DIR)
  python3 tools/install_hooks.py --config-dir D  instala no D/settings.json (ex.: ~/.claude-minha-org)
  python3 tools/install_hooks.py --remove        tira os hooks do quadro

Idempotente: rodar de novo não duplica. Antes de gravar, guarda uma cópia em settings.json.bak-kanban.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hook.py"
# evento -> matcher (None = evento sem matcher)
EVENTS = {"PreToolUse": "*", "UserPromptSubmit": None, "PostToolUse": "*", "PostToolUseFailure": "*",
          "PermissionRequest": "*", "Notification": None, "Stop": None, "StopFailure": None, "SessionEnd": None}


def is_ours(h):
    return h.get("type") == "command" and h.get("command", "").rstrip().endswith("hook.py") and "kanban" in h["command"]


def strip(hooks):
    """Tira os hooks do quadro (de qualquer instalação anterior) e os grupos que ficarem vazios."""
    out = {}
    for ev, groups in hooks.items():
        keep = []
        for g in groups:
            hs = [h for h in g.get("hooks", []) if not is_ours(h)]
            if hs:
                keep.append({**g, "hooks": hs})
        if keep:
            out[ev] = keep
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--python", default=sys.executable, help="interpretador que roda o hook (padrão: este)")
    a = ap.parse_args()

    f = Path(a.config_dir).expanduser() / "settings.json"
    try:
        s = json.loads(f.read_text()) if f.exists() else {}
    except ValueError:
        sys.exit(f"{f} não é um JSON válido; corrija antes de instalar.")
    hooks = strip(s.get("hooks") or {})
    if not a.remove:
        cmd = f"{a.python} {HOOK}"
        for ev, matcher in EVENTS.items():
            g = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
            if matcher:
                g = {"matcher": matcher, **g}
            hooks.setdefault(ev, []).append(g)
    if hooks:
        s["hooks"] = hooks
    else:
        s.pop("hooks", None)
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():
        shutil.copy2(f, f.with_name("settings.json.bak-kanban"))
    f.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")
    print(("Hooks removidos de " if a.remove else "Hooks instalados em ") + str(f))
    if not a.remove:
        print("Sessões do Claude já abertas só passam a usar os hooks quando forem reiniciadas.")


if __name__ == "__main__":
    main()
