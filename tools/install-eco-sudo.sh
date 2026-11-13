#!/bin/bash
# Libera, sem senha, SÓ os dois comandos de memória que o modo economia do quadro usa (liberar o cache de disco do Linux
# e compactar a memória, para o WSL devolver RAM ao Windows). Rodar uma vez:  sudo bash <este arquivo>
# Desfazer:  sudo rm /etc/sudoers.d/kanban-eco
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Rode com sudo: sudo bash $0"; exit 1; }
user=${SUDO_USER:?rode com sudo a partir do seu usuário}
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
echo "$user ALL=(root) NOPASSWD: /usr/sbin/sysctl -w vm.drop_caches=3, /usr/sbin/sysctl -w vm.compact_memory=1" > "$tmp"
visudo -cf "$tmp"  # confere antes de instalar: regra quebrada em /etc/sudoers.d pode travar o sudo
install -m 440 -o root -g root "$tmp" /etc/sudoers.d/kanban-eco
echo "Regra instalada em /etc/sudoers.d/kanban-eco"
