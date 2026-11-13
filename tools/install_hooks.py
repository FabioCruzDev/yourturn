#!/usr/bin/env python3
"""Instala (ou remove) os hooks do Claude Code que contam ao quadro se cada sessão está trabalhando ou esperando você.

  python3 tools/install_hooks.py                 instala no ~/.claude/settings.json (ou no $CLAUDE_CONFIG_DIR)
  python3 tools/install_hooks.py --config-dir D  instala no D/settings.json (ex.: ~/.claude-minha-org)
  python3 tools/install_hooks.py --remove        tira os hooks do quadro
  python3 tools/install_hooks.py --status        mostra o estado no ~/.claude e em cada organização (orgs.tsv)
  python3 tools/install_hooks.py --ensure        instala onde ainda não há nenhum hook do quadro (o ./start.sh usa);
                                                 hook antigo ou incompleto só gera aviso (corrija pelo quadro)

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
ORGS_FILE = Path("~/.config/claude-orgs/orgs.tsv")
STATUS_TEXT = {"ok": "ok", "missing": "sem os hooks do quadro", "partial": "hooks do quadro incompletos",
               "stale": "hooks apontando para outro hook.py", "invalid": "settings.json inválido"}


def is_ours(h, hook=HOOK):
    """Hook do quadro: o comando roda este hook.py (qualquer interpretador) ou um hook.py de outra instalação do quadro
    (pasta com "kanban" ou "yourturn": cópia antiga, worktree)."""
    cmd = h.get("command", "").rstrip() if h.get("type") == "command" else ""
    return cmd.endswith(str(hook)) or (cmd.endswith("/hook.py") and any(x in cmd.lower() for x in ("kanban", "yourturn")))


def strip(hooks, hook=HOOK):
    """Tira os hooks do quadro (de qualquer instalação anterior) e os grupos que ficarem vazios."""
    out = {}
    for ev, groups in hooks.items():
        keep = []
        for g in groups:
            hs = [h for h in g.get("hooks", []) if not is_ours(h, hook)]
            if hs:
                keep.append({**g, "hooks": hs})
        if keep:
            out[ev] = keep
    return out


def read(config_dir):
    f = Path(config_dir).expanduser() / "settings.json"
    return f, (json.loads(f.read_text()) if f.exists() else {})


def status(config_dir, hook=HOOK):
    """"ok" (todos os eventos chamam este hook.py), "missing" (nenhum hook do quadro), "partial" (faltam eventos),
    "stale" (algum aponta para outro hook.py ou para um que não existe) ou "invalid" (settings.json quebrado)."""
    try:
        _, s = read(config_dir)
        ours = {ev: [h["command"].rstrip() for g in groups for h in g.get("hooks", []) if is_ours(h, hook)]
                for ev, groups in (s.get("hooks") or {}).items()}
    except (OSError, ValueError, AttributeError, TypeError, KeyError):
        return "invalid"  # JSON quebrado ou com forma inesperada (lista no topo, "hooks": [])
    cmds = [c for v in ours.values() for c in v]
    if not cmds:
        return "missing"
    if any(not c.endswith(str(hook)) for c in cmds) or not Path(hook).exists():
        return "stale"
    return "ok" if all(ours.get(ev) for ev in EVENTS) else "partial"


def install(config_dir, python=sys.executable, remove=False, hook=HOOK):
    """Grava os hooks do quadro (ou tira, com remove) no settings.json da pasta. Devolve o arquivo."""
    f, s = read(config_dir)
    hooks = strip(s.get("hooks") or {}, hook)
    if not remove:
        cmd = f"{python} {hook}"
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
    return f


def python_in_use(dirs, default, hook=HOOK):
    """O interpretador de um hook do quadro que já funciona (senão `default`). Toda pasta precisa do MESMO comando: o
    settings de uma organização é a base mesclada com o dela (listas sem repetir), e comandos diferentes rodariam 2x."""
    for d in dirs:
        if status(d, hook) == "ok":
            for g in read(d)[1]["hooks"].get("Stop", []):
                for h in g.get("hooks", []):
                    if is_ours(h, hook):
                        return h["command"].rstrip()[:-len(str(hook))].strip() or default
    return default


def config_dirs(base="~/.claude", orgs_file=ORGS_FILE):
    """Pastas de config do Claude que o quadro usa: a base e a de cada organização do orgs.tsv, sem repetir."""
    out = [Path(base).expanduser()]
    try:
        for line in Path(orgs_file).expanduser().read_text().splitlines():
            f = line.split("\t")
            if len(f) == 3 and f[0] and not f[0].startswith("#"):
                out.append(Path(f[1]).expanduser())
    except OSError:
        pass
    return list(dict.fromkeys(out))


def show(d):
    return str(d).replace(str(Path.home()), "~", 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", default=os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ensure", action="store_true")
    ap.add_argument("--python", default=sys.executable, help="interpretador que roda o hook (padrão: este)")
    a = ap.parse_args()

    if a.status or a.ensure:
        warn, dirs = False, config_dirs()
        python = python_in_use(dirs, a.python)
        for d in dirs:
            st = status(d)
            if a.ensure and st == "missing":
                install(d, python)
                print(f"Hooks do quadro instalados em {show(d)}/settings.json")
            elif st != "ok":
                warn = warn or a.ensure
                print(f"{show(d)}: {STATUS_TEXT[st]}")
            elif a.status:
                print(f"{show(d)}: ok")
        if warn:
            print("O quadro mostra o aviso com o botão para corrigir (ou: python3 tools/install_hooks.py --config-dir <pasta>).")
        return
    try:
        f = install(a.config_dir, a.python, a.remove)
    except ValueError:
        sys.exit(f"{Path(a.config_dir).expanduser() / 'settings.json'} não é um JSON válido; corrija antes de instalar.")
    print(("Hooks removidos de " if a.remove else "Hooks instalados em ") + str(f))
    if not a.remove:
        print("Sessões do Claude já abertas só passam a usar os hooks quando forem reiniciadas.")


if __name__ == "__main__":
    main()
