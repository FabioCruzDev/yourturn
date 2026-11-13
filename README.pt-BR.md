# YourTurn

**Rode várias sessões do Claude Code. Saiba quando é a sua vez.**

Um quadro Kanban local para tocar várias sessões do [Claude Code](https://docs.claude.com/en/docs/claude-code) ao
mesmo tempo. Issues do Linear / Jira e tarefas locais viram cards; cada card abre uma sessão do Claude Code no tmux, e o
quadro mostra quais sessões estão trabalhando, quais estão **esperando você**, e deixa responder por um terminal
embutido no navegador (ou no celular).

*[Read in English](README.md)*

- **Raias**: A fazer · Claude trabalhando · Aguardando você · Em revisão · Concluído (7 dias). O card anda sozinho pelo estado da
  sessão (hooks do Claude Code + o que está na tela do tmux); também dá para mover à mão.
- **Sessões por card**: inicia o Claude Code num repositório (opcionalmente num worktree por card), retoma, hiberna
  sessões paradas, manda a mesma mensagem para várias sessões, lembrete que reabre o terminal de quem continua esperando.
- **Terminal embutido** (xterm.js por WebSocket até a janela do tmux), envio de arquivos, maximizar/lado a lado.
- **Ferramentas**: Linear e/ou Jira (Cloud ou Data Center), GitHub ou Bitbucket (selos de PR: checks, reviews,
  comentários), Slack (DMs e menções numa lateral, resposta pelo quadro), resumo por card com o Claude Haiku.
- **Organizações**: separa várias contas (ex.: dois empregos e o pessoal): cada uma tem `CLAUDE_CONFIG_DIR`, login do
  GitHub, e-mail de commit, credenciais de Linear/Jira/Slack e pastas de projeto próprios.
- **Celular**: PWA instalável com notificações Web Push ("a sessão X está esperando você"), via Tailscale.
- **Página de recursos** (`/recursos`): CPU, memória, discos, Docker, quem está consumindo (agrupado por sessão do
  Claude), ações com trava (encerrar, parar), limpeza de disco (worktrees antigos, caches). No WSL mostra também o
  lado do Windows.
- **Idiomas**: Português (Brasil) e English (US), nas Configurações.

Sem dependências: biblioteca padrão do Python + um arquivo HTML. Sem build.

## Requisitos

- Linux ou Windows com WSL2 (macOS não testado: a página de recursos lê o `/proc`).
- Python 3.10+, `tmux`, `git` e o `claude` (CLI) logado.
- Opcionais: `gh` (PRs do GitHub), `docker`, `python3-cryptography` (notificações push).

## Instalação

```bash
git clone https://github.com/FabioCruzDev/yourturn.git ~/yourturn
cd ~/yourturn
python3 tools/install_hooks.py   # põe os hooks do quadro no ~/.claude/settings.json (idempotente, guarda backup)
./start.sh                       # sobe o quadro no tmux; abra http://localhost:8765
```

`tools/install_hooks.py --remove` tira os hooks. Sessões já abertas só passam a usar os hooks depois de reiniciadas.
Para subir no login, chame o `start.sh` na inicialização (unit de usuário do systemd, `.profile` ou, no Windows, um
atalho para `wsl.exe -- bash -lc "~/yourturn/start.sh"`).

Configuração é opcional: copie `local.env.example` para `local.env` (fora do git) e ajuste porta, pasta dos repositórios
etc.

**Onde as sessões começam:** a sessão de um card abre num dos seus repositórios git: as pastas logo abaixo de
`KANBAN_REPOS_ROOT` (padrão `~/Projects`, ex.: `~/Projects/meu-app/.git`) mais as pastas de projeto de cada organização.
Clone seus repositórios ali, ou aponte `KANBAN_REPOS_ROOT` para a pasta onde eles estão.

## Conectar suas ferramentas

**Rápido (uma conta só):** ponha uma chave pessoal de API do Linear em `~/.config/kanban-claude/linear_key` (ou exporte
`LINEAR_API_KEY`). As issues atribuídas a você viram cards. Tarefas locais (“+ Nova tarefa”) funcionam sem ferramenta
nenhuma.

**Organizações (recomendado com mais de uma conta):** no quadro, abra o seletor de organização → “+ Nova organização…”:

| Campo | O que faz |
| --- | --- |
| Identificador, nome, cor, pastas de projeto | sessões iniciadas nessas pastas pertencem à organização |
| Chave do Linear / URL + token do Jira | issues atribuídas a você viram cards (só leitura) |
| Org do GitHub ou Bitbucket | selos de PR nos cards; “Entrar no GitHub” / “Configurar Bitbucket” abrem um terminal para o login |
| E-mail de commit | identidade do `git` dentro das pastas da organização |
| Token de usuário do Slack (`xoxp-…`) | DMs e menções na lateral (os escopos do app estão no formulário) |

Depois clique em “Entrar no Claude” para a organização (ela ganha o próprio `CLAUDE_CONFIG_DIR`, `~/.claude-<id>`).
Sessões em pastas que não são de nenhuma organização continuam usando o login normal do `~/.claude`.
As credenciais ficam em `~/.config/claude-orgs/<id>/` com permissão 600 e não saem da sua máquina.

> **O que criar uma organização muda fora do quadro:** cria `~/.claude-<id>` (configurações genéricas, hooks, commands e
> plugins entram por link a partir do `~/.claude`), acrescenta um bloco gerenciado no `~/.bashrc` para o `claude` e o
> `gh` escolherem a organização pela pasta, e acrescenta `includeIf "gitdir:<pasta>/"` no `~/.gitconfig` para o e-mail de
> commit e a credencial do GitHub. O `tools/claude_orgs.py` (`verify`, `sync`, `add`, `env`) faz o mesmo pela linha de
> comando; `apply`/`undo` dividem um `~/.claude` existente em organizações (com backup completo antes).

## Segurança

O quadro **digita nos seus terminais**. Acesso a ele equivale a acesso ao shell.

- O servidor só ouve em `127.0.0.1` e confere `Host` e `Origin` (proteção contra DNS rebinding e CSRF).
- Para acesso remoto use uma rede privada, ex.: `tailscale serve --bg 8765` e `KANBAN_HOSTS=<pc>.<tailnet>.ts.net` no
  `local.env`. **Nunca** faça bind em `0.0.0.0`, nem use `tailscale funnel` ou um túnel público.
- Os tokens ficam em arquivos com permissão 600 em `~/.config`; o quadro só os manda para a API da própria ferramenta.

## Extras opcionais

- **Modo economia / “Liberar cache”** (página de recursos) pode liberar o cache de disco do Linux; precisa de uma regra
  de sudo restrita: `sudo bash tools/install-eco-sudo.sh` (libera só os dois comandos `sysctl` usados).
- `KANBAN_DISKS="Dados=/mnt/dados"` mostra discos extras na página de recursos.
- Referências curtas de PR como `api#12` podem ser resolvidas para uma org do GitHub: `KANBAN_GH_ORG`, `KANBAN_PR_SHORT`.
- `KANBAN_VOICE=1` liga o “Jarvis”, assistente de voz experimental (reconhecimento de fala do navegador + `claude -p`).
  Desligado por padrão e sem suporte.

## Desenvolvimento

```bash
python3 test_server.py && python3 test_resources.py && python3 tools/test_bb_pr.py
```

Os testes não tocam nos seus dados reais quando rodam com um `HOME` descartável e `KANBAN_TMUX=kanban-test`. Os textos da
tela são escritos em português e traduzidos pelo `i18n_en.json` (`t("...")` nas páginas, `tr("...")` no servidor): ao
criar um texto visível, inclua a entrada em inglês.

## Licença

MIT, ver [LICENSE](LICENSE). O xterm.js incluído: ver [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
