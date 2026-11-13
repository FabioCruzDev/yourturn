"""Testes do resources.py (sem pytest): `/usr/bin/python3 test_resources.py`."""
import os
import subprocess
import time

import resources as r

# CPU: % de uso por núcleo a partir de dois instantes do /proc/stat
assert r.cpu_pct([(100, 50), (100, 100)], [(200, 75), (200, 150)]) == [75.0, 50.0]

# Agrupamento: cada processo conta num grupo só; a sessão leva a árvore dela (shell, testes); Jarvis, quadro, sistema
U = r.UID
ps = {
    1: {"pid": 1, "ppid": 0, "comm": "init", "cmd": "/sbin/init", "rss": 10, "uid": 0},
    10: {"pid": 10, "ppid": 1, "comm": "tmux: server", "cmd": "tmux new-session -d -s kanban -n voz cd tts && piper_server.py", "rss": 5, "uid": U},
    20: {"pid": 20, "ppid": 10, "comm": "claude", "cmd": "claude --resume 11111111-0000-0000-0000-000000000001", "rss": 300, "uid": U},
    21: {"pid": 21, "ppid": 20, "comm": "bash", "cmd": "bash -c pytest", "rss": 40, "uid": U},
    30: {"pid": 30, "ppid": 1, "comm": "python", "cmd": "piper-venv/bin/python piper_server.py", "rss": 150, "uid": U},
    31: {"pid": 31, "ppid": 40, "comm": "claude", "cmd": "claude -p --input-format stream-json", "rss": 190, "uid": U},
    40: {"pid": 40, "ppid": 10, "comm": "python3", "cmd": "/usr/bin/python3 server.py", "rss": 50, "uid": U},
    50: {"pid": 50, "ppid": 1, "comm": "sleep", "cmd": "sleep 900", "rss": 1, "uid": U},
    60: {"pid": 60, "ppid": 1, "comm": "vim", "cmd": "vim notas", "rss": 12, "uid": U},
    70: {"pid": 70, "ppid": 1, "comm": "postgres", "cmd": "postgres", "rss": 140, "uid": 999},
}
r.hooks["board_pid"] = 40
r._last["protected"] = r.protected(ps)
g = {x["kind"] + ":" + x["key"]: x for x in r.group(ps, {"11111111-0000-0000-0000-000000000001": {"issue": "T-1", "title": "Deploy", "org": "acme", "window": "T-1-1111", "status": "working"}}, [50], 40)}
assert g["session:11111111-0000-0000-0000-000000000001"]["rss"] == 340 and g["session:11111111-0000-0000-0000-000000000001"]["label"] == "T-1 · Deploy"
assert g["jarvis:jarvis"]["rss"] == 340  # voz (150) + cérebro claude -p (190), mesmo o cérebro sendo filho do quadro
assert g["board:board"]["rss"] == 50 and g["leftover:leftovers"]["rss"] == 1
assert {p["pid"] for p in g["mine:mine"]["procs"]} == {10, 60}  # tmux e vim
assert {p["pid"] for p in g["system:system"]["procs"]} == {1, 70}
# Travas: tmux, quadro e os ancestrais de cada Claude nunca encerráveis; Claude só pelo card; de outro usuário, nunca
can = {p["pid"]: p["can_kill"] for x in g.values() for p in x["procs"]}
assert can == {1: False, 10: False, 20: False, 21: True, 30: True, 31: False, 40: False, 50: True, 60: True, 70: False}, can

# kill de verdade: encerra um processo seu; recusa processo do sistema e o Claude (que se encerra pelo card)
p = subprocess.Popen(["sleep", "60"])
time.sleep(0.2)
assert r.kill({"pid": p.pid})["ok"] and p.wait(timeout=5) is not None
claude_pid = next((x for x, v in r.processes().items() if v["comm"] == "claude"), None)
for bad in [1] + ([claude_pid] if claude_pid else []):  # sistema (root) e sessão do Claude (só pelo card)
    try:
        r.kill({"pid": bad}); raise AssertionError(f"encerrou {bad}")
    except ValueError:
        pass
print("ok")

# ---------- limpeza de disco ----------
import shutil
import tempfile
from pathlib import Path

assert r.card_in("task/t-15-board", {"T-1"}) is None and r.card_in("task/t-15-board", {"T-15"}) == "T-15"
assert r.card_in("dev/acm-9101-plano", {"ACM-9101"}) == "ACM-9101"
d = Path(tempfile.mkdtemp())
run = lambda *a, cwd=None: subprocess.run(a, cwd=cwd, check=True, capture_output=True)
run("git", "init", "-q", str(d / "repo"))
run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x", cwd=d / "repo")
for name, branch in (("repo-t-9", "task/t-9-pronta"), ("repo-t-8", "task/t-8-suja"), ("repo-t-7", "task/t-7-aberta")):
    run("git", "-C", str(d / "repo"), "worktree", "add", "-q", "-b", branch, str(d / name))
(d / "repo-t-8" / "novo.txt").write_text("alteração não salva")
r.hooks.update(repos=lambda: [str(d / "repo"), str(d / "repo-t-9")], done_ids=lambda: {"T-9"},
               live_cwds=lambda: [str(d / "repo-t-7" / "sub")])
wt = {Path(i["id"]).name: i for i in r.worktrees()}
assert set(wt) == {"repo-t-9", "repo-t-8", "repo-t-7"}, wt  # o checkout principal não entra
assert wt["repo-t-9"]["suggest"] and wt["repo-t-9"]["why"] == ["card T-9 concluído"] and not wt["repo-t-9"]["block"]
assert wt["repo-t-8"]["block"] == "tem alteração não salva" and wt["repo-t-7"]["block"] == "sessão aberta nele"
r._clean["data"] = {"items": list(wt.values())}
assert r.cleanup({"kind": "worktree", "id": str(d / "repo-t-9")})["ok"] and not (d / "repo-t-9").exists()
for bad in ("repo-t-8", "repo-t-7"):  # trava repetida na hora de remover, mesmo que a tela tenha mostrado o botão
    try:
        r.cleanup({"kind": "worktree", "id": str(d / bad)}); raise AssertionError(f"removeu {bad}")
    except ValueError:
        pass
assert (d / "repo-t-8" / "novo.txt").exists() and (d / "repo-t-7").exists()
try:
    r.cleanup({"kind": "worktree", "id": "/etc"}); raise AssertionError("aceitou item fora da análise")
except ValueError:
    pass
# cache: apaga só a pasta da lista
cache = d / "cache-falso"; (cache / "x").mkdir(parents=True)
r.CACHES[:] = [("cache-x", "Cache falso", str(cache))]
r._clean["data"] = {"items": [{"kind": "cache", "id": "cache-x"}]}
r.cleanup({"kind": "cache", "id": "cache-x"}); assert not cache.exists()
shutil.rmtree(d, ignore_errors=True)
print("ok (limpeza)")
