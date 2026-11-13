"""python3 test_server.py — checa a heurística de estado das sessões."""
import server
from server import decide_status, last_prompt, safe_name, screen_status, summarize, voice_actions

base = {"sessionId": "s1", "cwd": "/x", "gitBranch": "dev/acm-1-a"}
asst = lambda content: {**base, "type": "assistant", "message": {"content": content}}
user = {**base, "type": "user", "message": {"content": "faz isso"}}
end = {**base, "type": "system", "subtype": "turn_duration"}
title = {"type": "agent-name", "agentName": "ACM-1 algo"}

s = summarize([title, user, asst([{"type": "text", "text": "pronto"}]), end], now=100, mtime=95)
assert s["status"] == "waiting" and s["last"] == "pronto" and s["title"] == "ACM-1 algo", s
assert summarize([user], 100, 95)["status"] == "working"
tool = asst([{"type": "tool_use", "name": "Bash"}])
assert summarize([user, tool], 100, 95)["status"] == "working"      # acabou de pedir a ferramenta
assert summarize([user, tool], 100, 50)["status"] == "waiting"      # parado há 50 s: permissão
assert summarize([user], 10_000, 0)["status"] == "idle"
assert summarize([{"type": "agent-name"}], 100, 95) is None
# Estado pela tela do tmux (o que vale para sessões do quadro)
box = "──── TIV ─\n❯ \n────────\n  auto mode"
assert screen_status("· Reticulating… (18m 32s · ↓ 25.0k tokens)\n\n" + box) == "working"
assert screen_status("✻ Baked for 21m 2s · done 11:21 PM\n" + box) == "waiting"
assert screen_status('grep… (1m 4s · 3 lines)\n✻ Baked for 2s\n' + box) == "waiting"  # saída de ferramenta antiga
assert screen_status("  4. Chat about this\nEnter to select · ↑/↓ to navigate · Esc to cancel\n──── TIV ─") == "waiting"
assert screen_status("iniciando…") is None
# Estado com hooks + tela
spin = "· Reticulating… (2m 3s · ↓ 1.2k tokens)\n  ⎿  Tip: use /clear\n" + box  # dica entre o spinner e a caixa
idle = "✻ Baked for 2m\n" + box
menu = "Do you want to proceed?\n❯ 1. Yes\nEsc to cancel"
H = lambda state, event, ts: {"state": state, "event": event, "ts": ts}
assert decide_status(spin, H("working", "PostToolUse", 90), 100) == "working"
assert decide_status(idle, H("waiting", "Stop", 99), 100) == "waiting"                 # Stop manda
assert decide_status(spin, H("waiting", "Stop", 99.5), 100) == "waiting"               # spinner residual logo após o Stop
assert decide_status(spin, H("waiting", "PermissionRequest", 50), 100) == "working"    # auto mode aprovou e a ferramenta roda
assert decide_status(menu + "\n" + box, H("working", "PreToolUse", 99), 100) == "waiting"  # pedido de permissão na tela
assert decide_status(idle, H("working", "PreToolUse", 95), 100) == "working"           # acabou de começar
assert decide_status(idle, H("working", "PreToolUse", 50), 100) == "waiting"           # Esc interrompeu (sem hook de fim)
assert decide_status(spin, None, 100) == "working" and decide_status(idle, None, 100) == "waiting"  # sem hook: só a tela
# Último prompt: pula resultado de ferramenta, mensagem interna e comando
recs = [{**base, "type": "user", "message": {"content": "primeiro pedido"}},
        {**base, "type": "user", "message": {"content": "corrige o teste do login"}},
        asst([{"type": "tool_use", "name": "Bash"}]),
        {**base, "type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {**base, "type": "user", "isMeta": True, "message": {"content": "caveat"}},
        {**base, "type": "user", "message": {"content": "<command-name>/clear</command-name>"}}]
assert last_prompt(recs) == "corrige o teste do login", last_prompt(recs)
# Nome do anexo: sem pasta, sem espaço (o caminho vai colado no prompt)
assert safe_name("../../etc/passwd") == "passwd"
assert safe_name("Captura de tela 2026.png") == "Captura_de_tela_2026.png"
assert safe_name("relatório (1).pdf") == "relatório_1_.pdf"
assert safe_name("...") == "arquivo" and safe_name("") == "arquivo"

# Comandos por voz: só ação conhecida, card existente e sessão aberta daquele card
cards = {"T-15": ["s-new", "s-old"], "ACM-1": []}
va = lambda *acts: voice_actions({"actions": list(acts)}, cards)
assert va({"type": "send", "card": "t-15", "text": "roda os testes"}) == [{"type": "send", "card": "T-15", "session": "s-new", "text": "roda os testes"}]
assert va({"type": "send", "card": "T-15", "session": "s-old", "text": "x"})[0]["session"] == "s-old"
assert va({"type": "send", "card": "T-15", "session": "s-de-outro-card", "text": "x"})[0]["session"] == "s-new"
assert va({"type": "send", "card": "ACM-1", "text": "x"}) == []          # card sem sessão aberta
assert va({"type": "send", "card": "T-99", "text": "x"}) == []           # card desconhecido
assert va({"type": "send", "card": "T-15", "text": " "}) == []
assert va({"type": "key", "card": "T-15", "key": "C-c"}) == []           # tecla fora da lista
assert va({"type": "key", "card": "T-15", "key": "Escape"})[0]["key"] == "Escape"
assert va({"type": "move", "card": "ACM-1", "lane": "review"}) == [{"type": "move", "card": "ACM-1", "lane": "review"}]
assert va({"type": "move", "card": "ACM-1", "lane": "lixo"}) == []
assert va({"type": "task_state", "card": "ACM-1", "state": "completed"}) == []  # Linear não se conclui pelo quadro
assert va({"type": "task_state", "card": "T-15", "state": "completed"})[0]["state"] == "completed"
assert va({"type": "create_task", "title": "Ligar pro suporte", "priority": 9})[0]["priority"] == 0
assert va({"type": "rm -rf"}, {"type": "summary"}, "lixo") == [{"type": "summary"}]
assert voice_actions("não é dict", cards) == [] and voice_actions({"actions": None}, cards) == []

# Blocos de código da última resposta (botões de copiar do celular)
import json as _json, tempfile as _tmp
from pathlib import Path as _P
_d = _P(_tmp.mkdtemp()); (_d / "proj").mkdir()
_sid = "12345678-1234-1234-1234-123456789abc"
_u = lambda t: {"type": "user", "message": {"content": t}}
_a = lambda t: {"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}
_tool = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "saída"}]}}
_recs = [_u("pedido antigo"), _a("```bash\nrm -rf velho\n```"), _u("faz o deploy"),
         _a("Rode:\n```bash\ngit tag -a v1 -m 'x'\ngit push --tags\n```"), _tool, _a("E depois:\n```\ncurl -s localhost:8765/\n```\nPronto.")]
(_d / "proj" / f"{_sid}.jsonl").write_text("\n".join(_json.dumps(r) for r in _recs))
server.PROJECTS = _d
_b = server.code_blocks(_sid)["blocks"]
assert [b["code"] for b in _b] == ["git tag -a v1 -m 'x'\ngit push --tags", "curl -s localhost:8765/"], _b
assert [b["lang"] for b in _b] == ["bash", ""], _b
try:
    server.code_blocks("../x"); raise AssertionError("aceitou id inválido")
except ValueError:
    pass

# Notificação: só quando a sessão PASSA a esperar; nada na 1ª rodada (reinício do servidor)
from server import new_waits
assert new_waits(None, {"a": "waiting"}) == []
assert new_waits({"a": "working"}, {"a": "waiting"}) == ["a"]
assert new_waits({"a": "waiting"}, {"a": "waiting"}) == []            # continua esperando: não repete
assert new_waits({}, {"b": "waiting"}) == ["b"]                        # sessão nova já esperando
assert new_waits({"a": "waiting"}, {"a": "working"}) == []
import push
assert push.allowed("https://fcm.googleapis.com/fcm/send/x") and push.allowed("https://wns2-bl2p.notify.windows.com/w/?token=x")
assert not push.allowed("https://evil.example/x") and not push.allowed("http://fcm.googleapis.com/x") and not push.allowed(None)

# Jarvis em fluxo: frases saem conforme chegam; o marcador de ações nunca vira fala; ações passam pelo filtro
from server import take_sentences
assert take_sentences("Pois não, senhor. Movo a T-2", 0) == (["Pois não, senhor."], 18)
assert take_sentences("Pois não, senhor. Movo a T-2", 18) == ([], 18)
assert take_sentences("Pois não, senhor. Movo a T-2.\nAÇ", 18) == (["Movo a T-2."], 30)
_events = []
def _fake_ask(context, text, on_delta):
    full = ""
    for piece in ["Pois não, ", "senhor. Mo", "vo a T-15 para ", "revisão.\nAÇ", "ÕES: [{\"type\": \"move\", ", "\"card\": \"T-15\", \"lane\": \"review\"}]"]:
        full += piece; on_delta(full)
    return full
server.BRAIN.ask = _fake_ask
server.voice_context = lambda body: ({"T-15": []}, ["- T-15 | raia: A fazer | quadro"])
server.voice_stream({"text": "move a quinze pra revisão"}, _events.append)
assert [e["say"] for e in _events if "say" in e] == ["Pois não, senhor.", "Movo a T-15 para revisão."], _events
assert _events[-1] == {"done": True, "actions": [{"type": "move", "card": "T-15", "lane": "review"}], "reply": "Pois não, senhor. Movo a T-15 para revisão."}, _events[-1]
# Organizações: pasta -> CLAUDE_CONFIG_DIR (a mais específica vence; fora delas, a ativa); antes do corte, nada muda
_o = _P(_tmp.mkdtemp())
server.SETTINGS = _o / "settings.json"  # nunca o settings.json real do quadro (a organização ativa muda com o uso)
server.ORGS_FILE = _o / "nao-existe.tsv"
assert server.orgs() == [] and server.org_config("/x") is None
_unset = " ".join(f"-u {v}" for v in server.ORG_VARS)
assert server.claude_cmd(["claude", "--resume", "a b"], None) == f"env {_unset} claude --resume 'a b'"
assert "CLAUDE_CONFIG_DIR" not in server.claude_env(None)
server.ORGS_FILE = _o / "orgs.tsv"
server.ORGS_FILE.write_text(f"# comentário\nacme\t{_o}/ct\t/p/Acme\npessoal\t{_o}/cp\t/p/Pessoal\nsub\t{_o}/cs\t/p/Acme/rpa/x\n")
assert server.org_config("/p/Acme/rpa") == _o / "ct"
assert server.org_config("/p/Acme") == _o / "ct"
assert server.org_config("/p/Acme/rpa/x/y") == _o / "cs"  # mais específica
_bl = server.base_logged
server.base_logged = lambda: False  # ~/.claude sem login (depois do corte): fora das pastas vale a organização ativa
assert server.org_config("/p/Acmex") == _o / "ct"  # não é subpasta: cai na organização ativa (acme)
server.base_logged = lambda: True  # instalação comum, ~/.claude logado: fora das pastas fica nele
assert server.org_config("/p/Acmex") is None and server.org_config("/p/Acme/rpa") == _o / "ct"
assert server.org_config() == _o / "ct"  # sem pasta (Jarvis, resumos): a organização ativa
server.base_logged = _bl
assert server.org_config("/p/Pessoal/app") == _o / "cp"
assert server.claude_cmd(["claude"], _o / "cp") == f"env {_unset} CLAUDE_CONFIG_DIR={_o}/cp claude"
assert server.claude_env(_o / "cp")["CLAUDE_CONFIG_DIR"] == str(_o / "cp")
# Vazamento: credenciais de quem subiu o quadro não chegam ao processo de uma organização; só as dela
(_o / "cp").mkdir()
(_o / "cp" / "settings.json").write_text(_json.dumps({"env": {"GH_CONFIG_DIR": f"{_o}/gh-p", "GIT_CONFIG_GLOBAL": f"{_o}/git-p"}}))
_saved = {k: server.os.environ.get(k) for k in server.ORG_VARS}
server.os.environ.update({"GH_TOKEN": "vazou", "GH_CONFIG_DIR": "/acme/gh", "LINEAR_API_KEY": "vazou", "CLAUDE_CONFIG_DIR": "/acme"})
_e = server.claude_env(_o / "cp")
assert _e["GH_CONFIG_DIR"] == f"{_o}/gh-p" and _e["GIT_CONFIG_GLOBAL"] == f"{_o}/git-p" and _e["CLAUDE_CONFIG_DIR"] == str(_o / "cp")
assert "GH_TOKEN" not in _e and "LINEAR_API_KEY" not in _e, _e
_e = server.claude_env(_o / "ct")  # organização sem env próprio: nada da outra, nada herdado
assert "GH_CONFIG_DIR" not in _e and "GH_TOKEN" not in _e and _e["CLAUDE_CONFIG_DIR"] == str(_o / "ct")
_cmd = server.claude_cmd(["claude"], _o / "cp")
assert "-u GH_TOKEN" in _cmd and f"GH_CONFIG_DIR={_o}/gh-p" in _cmd and "vazou" not in _cmd, _cmd
for k, v in _saved.items():
    server.os.environ.pop(k, None) if v is None else server.os.environ.__setitem__(k, v)
# Nome/cor do org.json (opcional); login = arquivo de credenciais do Claude na pasta da organização
(_o / "pessoal").mkdir(); (_o / "pessoal" / "org.json").write_text('{"name": "Minha", "color": "#123456"}')
(_o / "ct").mkdir(exist_ok=True); (_o / "ct" / ".credentials.json").write_text("{}")
_ol = {o["id"]: o for o in server.org_list()}
assert (_ol["pessoal"]["name"], _ol["pessoal"]["color"], _ol["acme"]["name"]) == ("Minha", "#123456", "Acme"), _ol
assert _ol["acme"]["login"] and not _ol["pessoal"]["login"]
assert server.org_name("pessoal") == "Minha" and server.org_name(None) == ""
# GitHub: None sem gh próprio; False sem conta no hosts.yml; True com conta
assert _ol["acme"]["gh_login"] is None and _ol["pessoal"]["gh_login"] is False
(_o / "gh-p").mkdir(); (_o / "gh-p" / "hosts.yml").write_text("github.com:\n    user: eu\n")
assert server.gh_logged(_o / "cp") is True
# Bitbucket: sem aviso de GitHub; a chave SSH da organização é o que conta
(_o / "pessoal" / "org.json").write_text('{"name": "Minha", "git_host": "bitbucket"}')
_p = {o["id"]: o for o in server.org_list()}["pessoal"]
assert (_p["git_host"], _p["gh_login"], _p["ssh_key"]) == ("bitbucket", None, (server.Path.home() / ".ssh" / "id_ed25519_pessoal").exists())
assert {o["id"]: o for o in server.org_list()}["acme"]["ssh_key"] is None
(_o / "pessoal" / "org.json").write_text('{"name": "Minha", "color": "#123456"}')
# Linear: uma chave por organização; quem não tem chave não usa o Linear; erro de uma não esconde as outras
(_o / "acme").mkdir(); (_o / "acme" / "linear_key").write_text("k-acme\n")
(_o / "pessoal" / "linear_key").write_text("k-pessoal")
_fetch = server.linear_fetch
server.linear_fetch = lambda key: ([{"identifier": "ACM-1"}], None) if key == "k-acme" else (None, "401")
server._linear_cache.clear()
_iss, _err = server.tracker_issues()
assert _iss == [{"identifier": "ACM-1", "org": "acme", "source": "linear"}] and _err == "Minha (Linear): 401", (_iss, _err)
(_o / "pessoal" / "linear_key").unlink(); server._linear_cache.clear()
assert server.tracker_issues() == ([{"identifier": "ACM-1", "org": "acme", "source": "linear"}], None)
(_o / "acme" / "linear_key").unlink(); server._linear_cache.clear()
assert server.tracker_issues()[0] is None and "Nenhuma organização com Linear ou Jira" in server.tracker_issues()[1]
server.linear_fetch = _fetch
# Jira: mesmo formato das issues do Linear; Cloud = e-mail + token (Basic, /rest/api/3/search/jql), Data Center = token
# pessoal (Bearer, /rest/api/2/search); subtarefa fica fora; categoria do status vira o estado
_reqs = []
class _Resp:
    def __init__(self, d): self.d = d
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self, *a): return _json.dumps(self.d).encode()
def _urlopen(req, timeout=0):
    _reqs.append(req)
    return _Resp({"issues": [
        {"key": "ACME-7", "fields": {"summary": "Corrigir login", "status": {"name": "Em progresso", "statusCategory": {"key": "indeterminate"}},
         "priority": {"name": "High"}, "created": "2026-10-30T09:00:00.000-0300", "updated": "2026-10-30T10:00:00.000-0300",
         "project": {"name": "Acme App"}, "issuetype": {"subtask": False}}},
        {"key": "ACME-8", "fields": {"summary": "sub", "issuetype": {"subtask": True}}}]})
_uo = server.urllib.request.urlopen
server.urllib.request.urlopen = _urlopen
_iss, _err = server.jira_fetch({"url": "https://acme.atlassian.net/", "email": "eu@acme.com", "token": "t0k"})
assert _err is None and len(_iss) == 1, (_iss, _err)
assert _iss[0] == {"identifier": "ACME-7", "title": "Corrigir login", "url": "https://acme.atlassian.net/browse/ACME-7",
                   "branchName": "acme-7-corrigir-login", "priority": 2, "priorityLabel": "High",
                   "createdAt": "2026-10-30T09:00:00.000-03:00", "updatedAt": "2026-10-30T10:00:00.000-03:00",
                   "state": {"name": "Em progresso", "type": "started"}, "team": {"name": "Acme App"}}, _iss[0]
assert _reqs[-1].full_url == "https://acme.atlassian.net/rest/api/3/search/jql"
assert _reqs[-1].get_header("Authorization") == "Basic " + server.base64.b64encode(b"eu@acme.com:t0k").decode()
assert _json.loads(_reqs[-1].data)["jql"] == server.JIRA_JQL
server.jira_fetch({"url": "https://jira.acme.com", "token": "pat", "jql": "project = X"})
assert _reqs[-1].full_url == "https://jira.acme.com/rest/api/2/search" and _reqs[-1].get_header("Authorization") == "Bearer pat"
assert _json.loads(_reqs[-1].data)["jql"] == "project = X"
def _401(req, timeout=0):
    raise server.urllib.error.HTTPError(req.full_url, 401, "x", {}, None)
server.urllib.request.urlopen = _401
assert server.jira_fetch({"url": "https://acme.atlassian.net", "email": "e", "token": "t"}) == (None, "HTTP 401 (confira e-mail e token)")
server.urllib.request.urlopen = _uo
# Bitbucket: repo pelo origin (SSH ou HTTPS), PRs por link na conversa e pela branch do card, selos como os do GitHub
import subprocess as _sp
_g = _P(_tmp.mkdtemp())
for _name, _url in (("bb", "git@bitbucket.org:globex/web-app.git"), ("bbh", "https://eu@bitbucket.org/globex/api.git"), ("gh", "git@github.com:acme-ai/acme-rpa.git")):
    _sp.run(["git", "init", "-q", str(_g / _name)]); _sp.run(["git", "-C", str(_g / _name), "remote", "add", "origin", _url])
assert [server.repo_of(str(_g / x)) for x in ("bb", "bbh", "gh")] == [("bitbucket", "globex/web-app"), ("bitbucket", "globex/api"), ("github", "acme-ai/acme-rpa")]
assert server.repo_of(str(_tmp.mkdtemp())) == ("", "")
_bbreq = []
def _bb_urlopen(req, timeout=0):
    _bbreq.append(req.full_url)
    u = req.full_url
    if "/pullrequests?q=" in u:
        return _Resp({"values": [{"id": 12}]})
    if u.endswith("/statuses?pagelen=50"):
        return _Resp({"values": [{"state": "SUCCESSFUL"}, {"state": "FAILED"}]})
    if u.endswith("/user"):
        return _Resp({"account_id": "me-1"})
    n = int(u.rsplit("/", 1)[1])
    return _Resp({"id": n, "title": f"PR {n}", "state": "OPEN", "author": {"account_id": "me-1" if n == 12 else "outro"},
                  "links": {"html": {"href": f"https://bitbucket.org/globex/web-app/pull-requests/{n}"}},
                  "participants": [{"approved": True}], "comment_count": 2})
server.urllib.request.urlopen = _bb_urlopen
_bb = {"email": "eu@globex.com", "token": "tk"}
_refs = server.card_prs("ZY-5", [{"cwd": str(_g / "bb"), "branch": "zy-5-login"}],
                        ["veja https://bitbucket.org/globex/web-app/pull-requests/40 e rpa#9"], short=False, bb=_bb)
assert _refs == [("bitbucket", "globex/web-app", 40), ("bitbucket", "globex/web-app", 12)], _refs
assert 'source.branch.name%3D%22zy-5-login%22' in _bbreq[0] and _bbreq[0].startswith("https://api.bitbucket.org/2.0/repositories/globex/web-app/pullrequests?q=")
_info = server.bb_pr_info("globex/web-app", 12, _bb)
assert _info == {"host": "bitbucket", "repo": "web-app", "number": 12, "url": "https://bitbucket.org/globex/web-app/pull-requests/12",
                 "title": "PR 12", "open": True, "author": "me-1",
                 "badges": [{"text": "teste quebrado (1)", "tone": "bad"}, {"text": "aprovado", "tone": "ok"}, {"text": "2 comentários", "tone": "muted"}]}, _info
assert server.bb_me(_bb) == "me-1"
assert server.bb_badges({"state": "MERGED"}, []) == [("mergeado", "ok")] and server.bb_badges({"state": "DECLINED"}, []) == [("fechado", "muted")]
assert server.bb_badges({"state": "OPEN", "draft": True, "participants": [{"state": "changes_requested"}]}, [{"state": "INPROGRESS"}]) == \
    [("rascunho", "muted"), ("testes rodando", "warn"), ("pedido de mudança", "bad")]
# branch que não é do card não busca PR; sem credencial do Bitbucket também não
_bbreq.clear()
assert server.card_prs("ZY-5", [{"cwd": str(_g / "bb"), "branch": "zy-9-outra"}], [], bb=_bb) == [] and not _bbreq
assert server.card_prs("ZY-5", [{"cwd": str(_g / "bb"), "branch": "zy-5-login"}], [], bb=None) == []
server.urllib.request.urlopen = _uo
# Linear numa organização e Jira em outra, juntos no quadro, cada um com a sua organização
(_o / "acme" / "linear_key").write_text("k-acme")
(_o / "pessoal" / "jira.json").write_text('{"url": "https://acme.atlassian.net", "email": "e", "token": "t"}')
_lf, _jf = server.linear_fetch, server.jira_fetch
server.linear_fetch = lambda key: ([{"identifier": "ACM-1"}], None)
server.jira_fetch = lambda c: ([{"identifier": "ACME-7"}], None) if c["url"].startswith("https://acme") else (None, "x")
server._linear_cache.clear()
assert server.tracker_issues() == ([{"identifier": "ACM-1", "org": "acme", "source": "linear"},
                                   {"identifier": "ACME-7", "org": "pessoal", "source": "jira"}], None)
server.linear_fetch, server.jira_fetch = _lf, _jf
(_o / "acme" / "linear_key").unlink(); (_o / "pessoal" / "jira.json").unlink(); server._linear_cache.clear()
# histórico de todas as organizações + a base; cada sessão lembra de qual conta veio (para o --resume)
for org, sid in (("ct", "aaaaaaaa-0000-0000-0000-000000000001"), ("cp", "aaaaaaaa-0000-0000-0000-000000000002")):
    (_o / org / "projects" / "proj").mkdir(parents=True)
    (_o / org / "projects" / "proj" / f"{sid}.jsonl").write_text("\n".join(_json.dumps(r) for r in _recs))
_found = {p.stem: p for p in server.session_files()}
assert {"aaaaaaaa-0000-0000-0000-000000000001", "aaaaaaaa-0000-0000-0000-000000000002", _sid} <= set(_found), _found
assert server.code_blocks("aaaaaaaa-0000-0000-0000-000000000002")["blocks"], "não achou o jsonl de outra organização"
# Jarvis troca de organização só para uma que existe; o contexto diz qual está ativa
assert [o["id"] for o in server.org_list()] == ["acme", "pessoal", "sub"]
assert server.org_list()[1]["paths"] == ["/p/Pessoal"]
assert server.voice_actions({"actions": [{"type": "org", "org": "Pessoal"}, {"type": "org", "org": "inexistente"}]}, {}) == [{"type": "org", "org": "pessoal"}]
server.load_settings = lambda: {"active_org": "pessoal"}
assert server.org_config("/fora") == _o / "cp"  # fora das pastas: a organização ativa
# Iniciar sessão: a organização é a da pasta; repositório de outra organização é recusado; pasta da organização sem
# git abre sem worktree
_L = _P(_tmp.mkdtemp())
(_L / "T" / "repo" / ".git").mkdir(parents=True); (_L / "Z").mkdir()
server.ORGS_FILE.write_text(f"acme\t{_o}/ct\t{_L}/T\nglobex\t{_o}/cz\t{_L}/Z\n")
_root, server.REPOS_ROOT = server.REPOS_ROOT, _L / "T"
assert server.org_of(f"{_L}/T/repo") == "acme" and server.org_of(f"{_L}/Z") == "globex" and server.org_of("/fora") is None
assert server.launch_dirs() == [f"{_L}/T/repo", f"{_L}/T", f"{_L}/Z"]  # repositórios + a pasta de cada organização
_calls = []
_tm, _et, _sl, _mw = server.tmux, server.ensure_tmux, server.save_launch, server.make_worktree
server.tmux, server.ensure_tmux = (lambda *a, **k: _calls.append(a)), (lambda: None)
server.save_launch = lambda *a: None
server.make_worktree = lambda *a: (_ for _ in ()).throw(AssertionError("criou worktree numa pasta sem git"))
try:
    server.launch({"issue": "ZY-5", "title": "x", "repo": f"{_L}/T/repo", "org": "globex", "prompt": "p"})
    raise AssertionError("abriu card da Globex num repositório da Acme")
except ValueError as e:
    assert "Esse repositório é da Acme; o card é da Globex" in str(e), e
assert not _calls
server.launch({"issue": "ZY-5", "title": "x", "repo": f"{_L}/Z", "org": "globex", "prompt": "p", "worktree": True, "branch": "zy-5"})
assert _calls[-1][:6] == ("new-window", "-d", "-t", f"{server.TMUX}:", "-n", _calls[-1][5]) and _calls[-1][7] == f"{_L}/Z"
assert f"CLAUDE_CONFIG_DIR={_o}/cz" in _calls[-1][8], _calls[-1]
server.tmux, server.ensure_tmux, server.save_launch, server.make_worktree, server.REPOS_ROOT = _tm, _et, _sl, _mw, _root
# Worktree: branch já aberta em outra worktree (criada a partir de uma pasta irmã) é reaproveitada, não recriada
import subprocess as _sp
_G = _P(_tmp.mkdtemp())
_g = lambda *a: _sp.run(["git", "-C", *a], check=True, capture_output=True)
_sp.run(["git", "init", "-q", "-b", "main", str(_G / "rp")], check=True)
_g(str(_G / "rp"), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
_g(str(_G / "rp"), "worktree", "add", "-q", "-b", "zy-9-x", str(_G / "rp-zy-1-zy-9"))
assert server.make_worktree(str(_G / "rp"), "zy-9-x", "ZY-9") == str(_G / "rp-zy-1-zy-9")
assert not (_G / "rp-zy-9").exists()
# Modo economia: ligar de novo (outro aparelho) não sobrescreve a lista das sessões guardadas para retomar
_eco_file, server.ECO = server.ECO, _o / "eco.json"
server.ECO.write_text('[{"id": "x", "issue": "T-1", "title": "S"}]')
server.SETTINGS.write_text('{"eco": true}')
_ls, server.load_settings = server.load_settings, lambda: {"eco": True}  # um teste acima trocou o load_settings
assert server.eco({"on": True}) == {"eco": True, "sessions": [], "failed": []}
assert server.ECO.read_text() == '[{"id": "x", "issue": "T-1", "title": "S"}]'
server.ECO, server.load_settings, _ = _eco_file, _ls, server.SETTINGS.write_text("{}")
# Pasta da sessão: o fim do jsonl tem registros de controle sem pasta (custo, último prompt); para retomar, vale a pasta
# onde a sessão nasceu, mesmo que uma retomada errada tenha gravado outra no fim
_rp = _P(_tmp.mkdtemp()); (_rp / "repo").mkdir()
_sid2 = "33333333-0000-0000-0000-000000000003"
_recs2 = [{"type": "user", "sessionId": _sid2, "cwd": str(_rp / "repo"), "gitBranch": "t-9", "message": {"content": "oi"}},
          {"type": "assistant", "sessionId": _sid2, "cwd": "/home/x", "gitBranch": "HEAD", "message": {"content": [{"type": "text", "text": "ok"}]}},
          {"type": "cost-state", "sessionId": _sid2}, {"type": "last-prompt", "sessionId": _sid2}]
_sm = server.summarize(_recs2, 0, 0)
assert (_sm["cwd"], _sm["branch"]) == ("/home/x", "HEAD"), _sm
(_d / "proj" / f"{_sid2}.jsonl").write_text("\n".join(_json.dumps(r) for r in _recs2))
_pj, server.PROJECTS = server.PROJECTS, _d
assert server.start_cwd(_sid2) == str(_rp / "repo") and server.start_cwd("nao-existe") == ""
server.PROJECTS = _pj
import tempfile
# Modo economia só encerra processos ÓRFÃOS no rascunho do Claude; filho de processo vivo (aqui, deste teste) fica
import os as _os, subprocess as _sp2, time as _time
if _os.path.isdir(f"/tmp/claude-{_os.getuid()}"):
    _rs = tempfile.mkdtemp(dir=f"/tmp/claude-{_os.getuid()}")
    _child = _sp2.Popen(["sleep", "30"], cwd=_rs)
    _sp2.run(["bash", "-c", "sleep 31 >/dev/null 2>&1 & disown"], cwd=_rs)  # o bash sai: o sleep 31 fica órfão
    _time.sleep(0.3)
    _lo = {x["cmd"]: x for x in server.leftovers()}
    assert "sleep 31" in _lo and "sleep 30" not in _lo, _lo
    _os.kill(_lo["sleep 31"]["pid"], 9); _child.kill(); _child.wait()
# Slack: DMs, grupos de DM e menções (fora as suas e as de canal sem menção); 1ª busca não avisa o acumulado;
# resposta só para conversa da lista, na thread quando é canal ou a mensagem estava numa
server.ORGS_FILE.write_text(f"acme\t{_o}/ct\t/p/Acme\n")
(_o / "acme").mkdir(exist_ok=True); (_o / "acme" / "slack.json").write_text('{"token": "xoxp-teste"}')
_sl_matches = [
    {"ts": "100.1", "user": "U2", "username": "ana", "text": "oi, pode ver?", "channel": {"id": "D1", "is_im": True}, "permalink": "https://x.slack.com/archives/D1/p1"},
    {"ts": "99.0", "user": "U3", "username": "bia", "text": "<@U1> revisa o PR", "channel": {"id": "C1", "name": "dev"}, "permalink": "https://x.slack.com/archives/C1/p2?thread_ts=98.0"},
    {"ts": "98.5", "user": "U4", "username": "caio", "text": "bom dia a todos", "channel": {"id": "C1", "name": "dev"}, "permalink": "p3"},
    {"ts": "98.0", "user": "U1", "username": "eu", "text": "minha msg", "channel": {"id": "D1", "is_im": True}, "permalink": "p4"},
    {"ts": "97.0", "user": "U5", "username": "duda", "text": "grupo", "channel": {"id": "G1", "is_mpim": True}, "permalink": "p5"},
    {"ts": "96.0", "user": "U9", "username": "linear", "text": "Issue atribuída a você", "channel": {"id": "D9", "is_im": True}, "permalink": "p7"},
    {"ts": "99.5", "user": "U1", "username": "eu", "text": "já olho", "channel": {"id": "C1", "name": "dev"}, "permalink": "https://x.slack.com/archives/C1/p9?thread_ts=98.0"}]
_sl_posts = []
def _sl_open(req, timeout=0):
    m = req.full_url.split("/api/")[1].split("?")[0]
    if m == "auth.test":
        return _Resp({"ok": True, "user_id": "U1"})
    if m == "users.info":
        u = server.parse_qs(server.urlparse(req.full_url).query)["user"][0]
        return _Resp({"ok": True, "user": {"is_bot": u == "U9", "real_name": {"U2": "Alice Silva", "U3": "Bob", "U5": "Carol", "U9": "Linear"}.get(u, u)}})
    if m == "search.messages":
        assert ("is%3Adm" in req.full_url or "%3C%40U1%3E" in req.full_url) and req.get_header("Authorization") == "Bearer xoxp-teste"
        return _Resp({"ok": True, "messages": {"matches": _sl_matches}})
    if m == "chat.postMessage":
        _sl_posts.append(_json.loads(req.data)); return _Resp({"ok": True})
    if m in ("conversations.history", "conversations.replies"):
        _sl_hist.append((m, server.parse_qs(server.urlparse(req.full_url).query)))
        return _Resp({"ok": True, "messages": [{"ts": "101.0", "user": "U2", "text": "e aí?"}, {"ts": "100.5", "user": "U1", "text": "oi <@U1>"},
                                               {"ts": "100.1", "user": "U2", "text": "oi, pode ver?"}]})
    raise AssertionError(m)
_sl_hist = []
server.SLACK_API, server.SLACK_SEEN = "https://slack.test/api", _o / "slack_seen.json"
server.urllib.request.urlopen = _sl_open
server._slack.clear()
assert server.slack_poll("acme") == []  # 1ª busca: não avisa o acumulado
_it = server._slack["acme"]["items"]
assert [(i["from"], i["where"]) for i in _it] == [("Alice Silva", "DM"), ("Bob", "#dev"), ("Carol", "grupo")], _it  # app (Linear) fora
assert _it[1]["text"] == "@você revisa o PR" and _it[1]["thread_ts"] == "98.0"
# ✓ respondido: thread da Bob tem resposta sua depois (99.5 > 99.0); DM da Alice tem a dela por último (100.1 > sua 98.0)
assert [(i["from"], i["replied"]) for i in _it] == [("Alice Silva", False), ("Bob", True), ("Carol", False)], _it
assert server.slack_state()["acme"]["unread"] == 2  # a da Bob já foi respondida: não conta
_sl_matches.insert(0, {"ts": "101.0", "user": "U2", "username": "ana", "text": "e aí?", "channel": {"id": "D1", "is_im": True}, "permalink": "p6"})
assert [i["ts"] for i in server.slack_poll("acme")] == ["101.0"]  # só a nova
server.slack_read({"orgs": ["acme"]}); assert server.slack_state()["acme"]["unread"] == 0
server.slack_reply({"org": "acme", "channel": "D1", "ts": "101.0", "text": " já vejo "})
server.slack_reply({"org": "acme", "channel": "C1", "ts": "99.0", "text": "ok"})
assert _sl_posts == [{"channel": "D1", "text": "já vejo"}, {"channel": "C1", "text": "ok", "thread_ts": "98.0"}], _sl_posts
for _bad in ({"org": "acme", "channel": "C999", "ts": "1", "text": "x"}, {"org": "acme", "channel": "D1", "ts": "101.0", "text": "  "}):
    try:
        server.slack_reply(_bad); raise AssertionError(f"respondeu {_bad}")
    except ValueError:
        pass
assert len(_sl_posts) == 2
# conversa: DM inteira em ordem, com você marcado; thread pela conversations.replies; menção solta só ela
_h = server.slack_history({"org": "acme", "channel": "D1", "ts": "101.0"})
assert _h["where"] == "conversa" and [(m["from"], m["me"], m["text"]) for m in _h["messages"]] == \
    [("Alice Silva", False, "oi, pode ver?"), ("Você", True, "oi @você"), ("Alice Silva", False, "e aí?")], _h
assert _sl_hist[-1][0] == "conversations.history" and _sl_hist[-1][1]["channel"] == ["D1"]
assert server.slack_history({"org": "acme", "channel": "C1", "ts": "99.0"})["where"] == "thread"
assert _sl_hist[-1][0] == "conversations.replies" and _sl_hist[-1][1]["ts"] == ["98.0"]
# menção em canal fora de thread: o canal até ela (latest=ts); sem a permissão, só ela com aviso
_sl_matches.append({"ts": "95.0", "user": "U3", "username": "bia", "text": "<@U1> e isso?", "channel": {"id": "C2", "name": "geral"}, "permalink": "p8"})
server._slack["acme"]["last"] = 0; server.slack_poll("acme")
_h = server.slack_history({"org": "acme", "channel": "C2", "ts": "95.0"})
assert _h["where"] == "canal" and _sl_hist[-1][0] == "conversations.history" and _sl_hist[-1][1]["latest"] == ["95.0"], (_h, _sl_hist[-1])
_ok_open = server.urllib.request.urlopen
def _noscope(req, timeout=0):
    if "conversations.history" in req.full_url:
        return _Resp({"ok": False, "error": "missing_scope"})
    return _ok_open(req, timeout)
server.urllib.request.urlopen = _noscope
_h = server.slack_history({"org": "acme", "channel": "C2", "ts": "95.0"})
assert _h["where"] == "menção" and len(_h["messages"]) == 1 and "channels:history" in _h["note"], _h
server.urllib.request.urlopen = _ok_open
# tirar da lista: some do estado e da contagem, sobrevive à próxima busca; fora da lista é recusado
server.SLACK_HIDDEN = _o / "slack_hidden.json"
server.slack_hide({"org": "acme", "channel": "D1", "ts": "101.0"})
assert "101.0" not in [i["ts"] for i in server.slack_state()["acme"]["items"]]
server.slack_poll("acme")
assert "101.0" not in [i["ts"] for i in server.slack_state()["acme"]["items"]] and server.slack_state()["acme"]["items"]
for _f, _a in ((server.slack_hide, {"org": "acme", "channel": "C9", "ts": "1"}), (server.slack_history, {"org": "acme", "channel": "C9", "ts": "1"})):
    try:
        _f(_a); raise AssertionError(f"aceitou {_a}")
    except ValueError:
        pass
server.urllib.request.urlopen = _uo; server._slack.clear(); (_o / "acme" / "slack.json").unlink()
# Slack pelo conector (MCP): o Claude devolve JSON; o quadro tira bots e você; resposta só com OK do conector
(_o / "acme" / "slack.json").write_text('{"via": "mcp", "every": 600}')
_mcp_calls = []
def _fake_mcp(org, prompt, tools, timeout=300):
    _mcp_calls.append((tools, prompt))
    if tools == [server.SLACK_MCP_SEARCH]:
        return 'Aqui: {"me": "U1", "items": [' + ",".join(_json.dumps(x) for x in [
            {"ts": "10.0", "channel_id": "D1", "channel_type": "im", "user_id": "U2", "user_name": "Alice", "is_bot": False, "thread_ts": "", "permalink": "l1", "text": "oi"},
            {"ts": "9.0", "channel_id": "C1", "channel_type": "channel", "channel_name": "dev", "user_id": "U3", "user_name": "Bob", "is_bot": False, "thread_ts": "8.0", "permalink": "l2", "text": "<@U1> olha"},
            {"ts": "7.0", "channel_id": "D9", "channel_type": "im", "user_id": "U9", "user_name": "Jira", "is_bot": True, "permalink": "l3", "text": "bot"},
            {"ts": "6.0", "channel_id": "D1", "channel_type": "im", "user_id": "U1", "user_name": "eu", "permalink": "l4", "text": "minha"}]) + "]}"
    return "OK"
_smcp, server.slack_mcp = server.slack_mcp, _fake_mcp
server._slack.clear()
assert server.slack_poll("acme") == []
_it = server._slack["acme"]["items"]
assert [(i["from"], i["where"], i["thread_ts"]) for i in _it] == [("Alice", "DM", ""), ("Bob", "#dev", "8.0")] and _it[1]["text"] == "@você olha", _it
assert server.slack_poll("acme") == [] and len(_mcp_calls) == 1  # dentro do intervalo: não consulta de novo
server.slack_reply({"org": "acme", "channel": "C1", "ts": "9.0", "text": "vejo já"})
assert _mcp_calls[-1][0] == [server.SLACK_MCP_SEND] and 'thread_ts "8.0"' in _mcp_calls[-1][1] and "<msg>vejo já</msg>" in _mcp_calls[-1][1]
server.slack_mcp = lambda *a, **k: "ERRO: channel_not_found"
try:
    server.slack_reply({"org": "acme", "channel": "D1", "ts": "10.0", "text": "x"}); raise AssertionError("aceitou erro do conector")
except ValueError as e:
    assert "channel_not_found" in str(e)
server.slack_mcp = _smcp; server._slack.clear(); (_o / "acme" / "slack.json").unlink()
# Trocar a organização de um card: guarda no board.json; vazio volta à de origem; organização desconhecida é recusada
server.ORGS_FILE.write_text(f"acme\t{_o}/ct\t/p/Acme\nglobex\t{_o}/cz\t/p/Globex\n")
_bf, server.BOARD = server.BOARD, _o / "board.json"
server.card_update({"id": "ACM-1", "org": "globex"}); assert server.load_board()["ACM-1"]["org"] == "globex"
server.card_update({"id": "ACM-1", "org": ""}); assert "org" not in server.load_board()["ACM-1"]
try:
    server.card_update({"id": "ACM-1", "org": "inexistente"}); raise AssertionError("aceitou organização desconhecida")
except ValueError:
    pass
server.BOARD = _bf
# Hibernação: só sessão com conversa gravada, aberta no quadro, sem trabalhar há mais de N horas
_now = 1_000_000.0
_hs = [{"id": "w", "window": "W", "config": "c", "status": "working", "updated": _now - 9 * 3600},
       {"id": "old", "window": "O", "config": "c", "status": "waiting", "updated": _now - 3 * 3600},
       {"id": "new", "window": "N", "config": "c", "status": "waiting", "updated": _now - 3600},
       {"id": "term", "window": "T", "status": "working", "updated": _now - 9 * 3600},  # terminal avulso: sem "config"
       {"id": "gone", "window": None, "config": "c", "status": "idle", "updated": _now - 9 * 3600}]
_killed = []
_ss, _tm, _ls = server.sessions, server.tmux, server.load_settings
server.sessions, server.tmux = (lambda: _hs), (lambda *a, **k: _killed.append(a[-1]))
server.HIBERNATED = _o / "hibernated.json"
server.load_settings = lambda: {"hibernate_hours": 2}
assert server.hibernate_idle(_now) == ["old"] and _killed == [f"{server.TMUX}:O"], _killed
assert "old" in server.hibernated()
server.load_settings = lambda: {"hibernate_hours": 0}
assert server.hibernate_idle(_now) == []
server.load_settings = lambda: {"hibernate_hours": 2, "eco": True}  # na economia já está tudo fechado
assert server.hibernate_idle(_now) == []
server.sessions, server.tmux, server.load_settings = _ss, _tm, _ls
server.ORGS_FILE = _o / "nao-existe.tsv"
print("ok")
