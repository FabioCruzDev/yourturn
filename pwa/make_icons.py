"""Gera os ícones do app (PNG puro, sem Pillow): quadrado com três colunas de kanban de alturas diferentes.
Uso: python3 pwa/make_icons.py   (escreve pwa/icon-192.png e pwa/icon-512.png; cabem no círculo do Android)"""
import struct, zlib
from pathlib import Path

BG, FG = (91, 91, 214), (255, 255, 255)  # a cor de destaque do quadro

def icon(n):
    bars = [(0.24, 0.30, 0.74), (0.43, 0.30, 0.58), (0.62, 0.30, 0.66)]  # (x, topo, base) em fração do lado
    w = 0.14
    rows = []
    for y in range(n):
        row = bytearray([0])
        for x in range(n):
            fx, fy = (x + .5) / n, (y + .5) / n
            on = any(bx <= fx < bx + w and top <= fy <= base for bx, top, base in bars)
            row += bytes(FG if on else BG)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    chunk = lambda t, d: struct.pack("!I", len(d)) + t + d + struct.pack("!I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", n, n, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")

for n in (192, 512):
    (Path(__file__).parent / f"icon-{n}.png").write_bytes(icon(n))
print("ok")
