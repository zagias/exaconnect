"""QR codes drawn on the server as SVG (ADR 0033).

A small, dependency-free QR Code Model 2 encoder: byte mode, error
correction level M (about 15% of the code can be damaged), versions 1 to 20
(up to 666 bytes, plenty for a softphone sign-in link). The mask is chosen
by the standard penalty rules. Tests check the output module for module
against an independent library.

The portal shows the SVG for a softphone sign-in link so a person can scan
it with the phone app instead of typing the link.
"""

from __future__ import annotations

from html import escape

# Error correction level M: (EC codewords per block, [(blocks, data codewords per block), ...]).
_M = {
    1: (10, [(1, 16)]),
    2: (16, [(1, 28)]),
    3: (26, [(1, 44)]),
    4: (18, [(2, 32)]),
    5: (24, [(2, 43)]),
    6: (16, [(4, 27)]),
    7: (18, [(4, 31)]),
    8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
    11: (30, [(1, 50), (4, 51)]),
    12: (22, [(6, 36), (2, 37)]),
    13: (22, [(8, 37), (1, 38)]),
    14: (24, [(4, 40), (5, 41)]),
    15: (24, [(5, 41), (5, 42)]),
    16: (28, [(7, 45), (3, 46)]),
    17: (28, [(10, 46), (1, 47)]),
    18: (26, [(9, 43), (4, 44)]),
    19: (26, [(3, 44), (11, 45)]),
    20: (26, [(3, 41), (13, 42)]),
}
MAX_VERSION = max(_M)
_ECL_BITS_M = 0  # format bits for level M


class QRError(ValueError):
    pass


# ---- Reed-Solomon over GF(256), polynomial 0x11D ------------------------------------------


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


# ---- layout helpers --------------------------------------------------------------------------


def _alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    num = ver // 7 + 2
    size = ver * 4 + 17
    step = 26 if ver == 32 else (ver * 4 + num * 2 + 1) // (num * 2 - 2) * 2
    out = [size - 7 - i * step for i in range(num - 1)] + [6]
    return sorted(out)


def _data_capacity(ver: int) -> int:
    _, groups = _M[ver]
    return sum(n * k for n, k in groups)


def _bits_for_count(ver: int) -> int:
    return 8 if ver <= 9 else 16


class _Matrix:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]

    def set_fn(self, x: int, y: int, dark: bool) -> None:
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def draw_function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        d = max(abs(dx), abs(dy))
                        self.set_fn(x, y, d not in (2, 4))
        pos = _alignment_positions(self.ver)
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        self.draw_version()

    def draw_format(self, mask: int) -> None:
        data = (_ECL_BITS_M << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        n = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 != 0

        for i in range(6):
            self.set_fn(8, i, bit(i))
        self.set_fn(8, 7, bit(6))
        self.set_fn(8, 8, bit(7))
        self.set_fn(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fn(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fn(8, n - 15 + i, bit(i))
        self.set_fn(8, n - 8, True)

    def draw_version(self) -> None:
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.ver << 12) | rem
        for i in range(18):
            dark = (bits >> i) & 1 != 0
            a, b = self.size - 11 + i % 3, i // 3
            self.set_fn(a, b, dark)
            self.set_fn(b, a, dark)

    def draw_codewords(self, data: list[int]) -> None:
        n, i = self.size, 0
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.mod[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 != 0
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if self.fn[y][x]:
                    continue
                if mask == 0:
                    inv = (x + y) % 2 == 0
                elif mask == 1:
                    inv = y % 2 == 0
                elif mask == 2:
                    inv = x % 3 == 0
                elif mask == 3:
                    inv = (x + y) % 3 == 0
                elif mask == 4:
                    inv = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    inv = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    inv = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    inv = ((x + y) % 2 + x * y % 3) % 2 == 0
                if inv:
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self) -> int:
        n, m = self.size, self.mod
        score = 0
        # Rule 1: runs of five or more of one colour, in rows and columns.
        for lines in (m, [list(col) for col in zip(*m, strict=True)]):
            for line in lines:
                run, prev = 0, None
                for v in line:
                    if v == prev:
                        run += 1
                    else:
                        if run >= 5:
                            score += run - 2
                        run, prev = 1, v
                if run >= 5:
                    score += run - 2
        # Rule 2: 2x2 blocks of one colour.
        for y in range(n - 1):
            for x in range(n - 1):
                c = m[y][x]
                if c == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        # Rule 3: finder-like patterns 1:1:3:1:1 with four light modules on one side.
        pats = ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1])
        for lines in (m, [list(col) for col in zip(*m, strict=True)]):
            for line in lines:
                bits = [1 if v else 0 for v in line]
                for x in range(n - 10):
                    window = bits[x : x + 11]
                    if window == pats[0] or window == pats[1]:
                        score += 40
        # Rule 4: balance of dark and light.
        dark = sum(v for row in m for v in row)
        total = n * n
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        score += k * 10
        return score


def _codewords(data: bytes) -> tuple[int, list[int]]:
    ver = next(
        (v for v in range(1, MAX_VERSION + 1) if 4 + _bits_for_count(v) + len(data) * 8 <= _data_capacity(v) * 8),
        None,
    )
    if ver is None:
        raise QRError(f"That text is too long for a QR code here (at most {_data_capacity(MAX_VERSION) - 3} bytes).")
    cap = _data_capacity(ver) * 8
    bits: list[int] = []

    def put(val: int, n: int) -> None:
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), _bits_for_count(ver))
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    raw = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    ec_len, groups = _M[ver]
    div = _rs_divisor(ec_len)
    blocks, ecs, k = [], [], 0
    for count, size in groups:
        for _ in range(count):
            blk = raw[k : k + size]
            k += size
            blocks.append(blk)
            ecs.append(_rs_remainder(blk, div))
    out = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec_len):
        out.extend(e[i] for e in ecs)
    return ver, out


def matrix(text: str, mask: int | None = None) -> list[list[bool]]:
    """The modules (True = dark), without the quiet zone."""
    ver, cw = _codewords(text.encode("utf-8"))
    best: tuple[int, _Matrix] | None = None
    for msk in range(8) if mask is None else (mask,):
        mx = _Matrix(ver)
        mx.draw_function_patterns()
        mx.draw_codewords(cw)
        mx.apply_mask(msk)
        mx.draw_format(msk)
        p = mx.penalty() if mask is None else 0
        if best is None or p < best[0]:
            best = (p, mx)
    assert best is not None
    return best[1].mod


def svg(text: str, *, label: str = "QR code", scale: int = 4, border: int = 4) -> str:
    m = matrix(text)
    n = len(m)
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(m) for x, v in enumerate(row) if v)
    size = n + 2 * border
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size * scale}" '
        f'height="{size * scale}" role="img" aria-label="{escape(label)}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#ffffff"/><path d="{path}" fill="#07182E"/></svg>'
    )
