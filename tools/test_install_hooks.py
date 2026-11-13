"""Teste do install_hooks.py só com pastas temporárias: `/usr/bin/python3 tools/test_install_hooks.py`."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_hooks as ih  # noqa: E402

tmp = Path(tempfile.mkdtemp())
hook = tmp / "quadro" / "hook.py"  # sem "kanban" no caminho, como numa instalação em ~/yourturn
hook.parent.mkdir()
hook.write_text("")
cfg = tmp / "claude"
ours = lambda d: [h["command"] for gs in ih.read(d)[1].get("hooks", {}).values() for g in gs for h in g["hooks"] if ih.is_ours(h, hook)]

assert ih.status(cfg, hook) == "missing"
cfg.mkdir()
(cfg / "settings.json").write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/check-secrets.py"}]},
    {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/python3 /home/u/yourturn/hook.py"}]}]}}))
assert ih.status(cfg, hook) == "stale"  # instalação antiga em outra pasta
ih.install(cfg, "/usr/bin/python3", hook=hook)
ih.install(cfg, "/usr/bin/python3", hook=hook)  # de novo: não duplica
s = ih.read(cfg)[1]
assert ih.status(cfg, hook) == "ok" and len(ours(cfg)) == len(ih.EVENTS), ours(cfg)
assert s["model"] == "x" and s["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("check-secrets.py")  # o resto fica
assert (cfg / "settings.json.bak-kanban").exists()
s["hooks"].pop("Stop")
(cfg / "settings.json").write_text(json.dumps(s))
assert ih.status(cfg, hook) == "partial"
ih.install(cfg, "/usr/bin/python3", remove=True, hook=hook)
assert ih.status(cfg, hook) == "missing" and ih.read(cfg)[1]["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
(cfg / "settings.json").write_text("{quebrado")
assert ih.status(cfg, hook) == "invalid"
hook.unlink()
(cfg / "settings.json").write_text("{}")
ih.install(cfg, "/usr/bin/python3", hook=hook)
assert ih.status(cfg, hook) == "stale"  # o hook.py sumiu (pasta do quadro apagada ou movida)

# Pastas: a base + as do orgs.tsv, sem repetir e sem comentários
tsv = tmp / "orgs.tsv"
tsv.write_text(f"# org\tconfig\tpastas\na\t{tmp}/ca\t/x\na\t{tmp}/ca\t/y\nb\t{cfg}\t/z\n")
assert ih.config_dirs(cfg, tsv) == [cfg, tmp / "ca"], ih.config_dirs(cfg, tsv)
assert ih.config_dirs(cfg, tmp / "nao-existe") == [cfg]

h = lambda c: {"type": "command", "command": c}
assert ih.is_ours(h("python3 /opt/kanban-claude/hook.py")) and ih.is_ours(h("/usr/bin/python3 /srv/x/hook.py"), Path("/srv/x/hook.py"))
assert not ih.is_ours(h("/x/outro/hook.py")) and not ih.is_ours({"type": "prompt", "command": "/opt/kanban/hook.py"})
# JSON válido com forma inesperada não derruba o /api/state
for weird in ("[]", '{"hooks": [1]}', '{"hooks": {"Stop": "x"}}', '{"hooks": {"Stop": [1]}}'):
    (cfg / "settings.json").write_text(weird)
    assert ih.status(cfg, hook) == "invalid", weird
hook.write_text("")
(cfg / "settings.json").write_text('{"hooks": []}')  # lista vazia = sem hooks: instala normalmente
assert ih.status(cfg, hook) == "missing"
ih.install(cfg, "/usr/bin/python3", hook=hook)
assert ih.status(cfg, hook) == "ok"

# --ensure numa organização sem hooks usa o interpretador da base (que já funciona): mesmo comando, nada roda 2x
base, org = tmp / "base", tmp / "org"
ih.install(base, "/opt/pyenv/bin/python3", hook=hook)
assert ih.python_in_use([org, base], "/usr/bin/python3", hook) == "/opt/pyenv/bin/python3"
assert ih.python_in_use([org], "/usr/bin/python3", hook) == "/usr/bin/python3"
ih.install(org, ih.python_in_use([base, org], "/usr/bin/python3", hook), hook=hook)
assert set(ours(base)) == set(ours(org)) == {f"/opt/pyenv/bin/python3 {hook}"}
sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_orgs  # noqa: E402
merged = claude_orgs.merge(ih.read(base)[1], ih.read(org)[1])  # o que o sync grava na organização
assert all(len(v) == 1 for v in merged["hooks"].values()), merged["hooks"]
print("ok")
