#!/bin/bash
# Sobe o quadro dentro do tmux "kanban", se ainda não estiver no ar. Pode rodar quantas vezes quiser.
# Na primeira vez também confere as dependências e instala os hooks do Claude Code (só onde não há nenhum do quadro).
# Configuração local (não versionada): local.env, no formato KEY=valor (ver local.env.example).
#   KANBAN_NO_HOOKS=1    não instala hooks (use se tirou com tools/install_hooks.py --remove)
#   KANBAN_NO_BROWSER=1  não abre o navegador (ele só abre quando o start.sh sobe o quadro num terminal)
set -u
cd "$(dirname "$0")"
[ -f local.env ] && { set -a; . ./local.env; set +a; }
PORT="${KANBAN_PORT:-8765}"
SESSION="${KANBAN_TMUX:-kanban}"
PY="${KANBAN_PYTHON:-$(command -v /usr/bin/python3 || command -v python3)}"
URL="http://localhost:$PORT"

# ---------- dependências: sem Python 3.10+ e tmux não sobe; o resto só avisa ----------
if command -v apt-get >/dev/null; then PKG="sudo apt install"
elif command -v dnf >/dev/null; then PKG="sudo dnf install"
elif command -v pacman >/dev/null; then PKG="sudo pacman -S"
elif command -v brew >/dev/null; then PKG="brew install"
else PKG=""
fi
how() { [ -n "$PKG" ] && echo "    $PKG $*"; }
missing=0
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "Falta o Python 3.10 ou mais novo$([ -n "$PY" ] && echo " (achei: $("$PY" --version 2>&1))")."; how python3; missing=1
fi
command -v tmux >/dev/null || { echo "Falta o tmux (o quadro guarda as sessões do Claude nele)."; how tmux; missing=1; }
[ "$missing" = 1 ] && { echo "Instale o que falta e rode ./start.sh de novo."; exit 1; }
command -v git >/dev/null || { echo "Aviso: sem git, o quadro não cria worktrees nem mostra branches."; how git; }
command -v claude >/dev/null || echo "Aviso: não achei o comando claude neste shell. Instale o Claude Code (https://claude.com/claude-code) e faça login uma vez com: claude"

# ---------- hooks do Claude Code: é por eles que o quadro sabe se cada sessão trabalha ou espera você ----------
[ "${KANBAN_NO_HOOKS:-}" = 1 ] || env -u CLAUDE_CONFIG_DIR "$PY" tools/install_hooks.py --ensure --python "$PY"

open_browser() {
  [ -t 1 ] && [ "${KANBAN_NO_BROWSER:-}" != 1 ] || return 0
  for _ in $(seq 20); do curl -s -o /dev/null -m 1 "$URL/" && break; sleep 0.5; done
  if grep -qi microsoft /proc/version 2>/dev/null; then (cd /mnt/c 2>/dev/null; explorer.exe "$URL" >/dev/null 2>&1) || true
  elif command -v open >/dev/null && [ "$(uname)" = Darwin ]; then open "$URL"
  elif command -v xdg-open >/dev/null && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then xdg-open "$URL" >/dev/null 2>&1 &
  fi
}

window() {  # window <nome> <comando>: (re)cria a janela no tmux
  if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux new-session -d -s "$SESSION" -n "$1" "$2"
  elif tmux list-windows -t "$SESSION" -F '#{window_name}' | grep -qx "$1"; then
    tmux respawn-window -k -t "$SESSION:$1" "$2"
  else
    tmux new-window -d -t "$SESSION:" -n "$1" "$2"
  fi
}

# Voz do Jarvis (experimental, só com KANBAN_VOICE=1 e o Piper instalado em tts/piper-venv)
if [ "${KANBAN_VOICE:-}" = 1 ] && [ -x tts/piper-venv/bin/python ] && ! curl -s -o /dev/null -m 2 "http://127.0.0.1:${KANBAN_TTS_PORT:-8766}/health"; then
  window voz "cd $(pwd)/tts && MEM=800M SWAP=0 ./limited.sh piper-venv/bin/python piper_server.py"
fi

if curl -s -o /dev/null -m 3 "http://127.0.0.1:$PORT/"; then
  echo "Quadro já está no ar: $URL"
  exit 0
fi
# A janela do tmux não herda este shell (o servidor do tmux já pode estar rodando): passa as KANBAN_* explicitamente.
ENVS=$(env | grep -E '^(KANBAN_|LINEAR_API_KEY=)' | while IFS= read -r kv; do printf "%q " "$kv"; done)
window quadro "cd $(printf %q "$(pwd)") && exec env $ENVS $(printf %q "$PY") server.py"
echo "Quadro subindo em $URL  (tmux attach -t $SESSION)"
open_browser
