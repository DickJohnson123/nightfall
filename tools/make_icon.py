"""Генерация иконки приложения (PNG) без внешних библиотек."""

import struct
import zlib
from pathlib import Path

SIZE = 128
SUPERSAMPLE = 4
VIOLET = (167, 139, 250)      # #A78BFA
VIOLET_DARK = (124, 92, 246)  # #7C5CFF


def inside_crescent(x: float, y: float) -> float:
    """Возвращает 0 или 1: точка внутри полумесяца."""
    cx = cy = SIZE / 2
    r_outer = SIZE * 0.40
    # вырезаем круг, смещённый вправо-вверх
    dx, dy = x - (cx + SIZE * 0.15), y - (cy - SIZE * 0.13)
    r_inner = SIZE * 0.34
    if (x - cx) ** 2 + (y - cy) ** 2 > r_outer ** 2:
        return 0.0
    if dx * dx + dy * dy < r_inner ** 2:
        return 0.0
    return 1.0


def build() -> bytes:
    rows = []
    step = 1.0 / SUPERSAMPLE
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            covered = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    covered += inside_crescent(px + (sx + 0.5) * step, py + (sy + 0.5) * step)
            alpha = int(255 * covered / (SUPERSAMPLE ** 2))
            # лёгкий вертикальный градиент цвета
            t = py / (SIZE - 1)
            r = int(VIOLET_DARK[0] + (VIOLET[0] - VIOLET_DARK[0]) * (1 - t))
            g = int(VIOLET_DARK[1] + (VIOLET[1] - VIOLET_DARK[1]) * (1 - t))
            b = int(VIOLET_DARK[2] + (VIOLET[2] - VIOLET_DARK[2]) * (1 - t))
            row += bytes((r, g, b, alpha))
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + row for row in rows)
    compressed = zlib.compress(raw)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    target = Path(__file__).resolve().parent.parent / "assets" / "nightfall.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build())
    print(f"готово: {target} ({target.stat().st_size} байт)")
