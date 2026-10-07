"""The server-side QR encoder (ADR 0039).

Each code is read back the way a scanner would: format bits checked and
decoded, the mask removed, codewords read in the zig-zag order and
de-interleaved, every block checked by its Reed-Solomon syndromes (worked
out independently of the encoder, by evaluating the block at the generator's
roots), and the byte-mode data decoded back to the text. The fixed patterns
are also compared with an independent library when it is installed.
"""

import pytest

from exaconnect_controller.commai import qr

from .commai_helpers import base, business

TEXTS = [
    "a",
    "https://connect.exacarib.com/softphone/join?t=" + "Kq7xZ" * 2,
    "https://connect.exacarib.com/softphone/join?t=" + "Kq7xZ" * 20,
    "Fête de la musique — São Tomé, Curaçao",
    "x" * 300,
    "y" * 660,
]


def _exp_log():
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


EXP, LOG = _exp_log()


def _mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def _syndromes(block, n_ec):
    out = []
    for i in range(n_ec):
        s = 0
        for c in block:
            s = _mul(s, EXP[i]) ^ c
        out.append(s)
    return out


def _format_ok(m):
    bits = 0
    for i in range(6):
        bits |= m[i][8] << i
    bits |= m[7][8] << 6 | m[8][8] << 7 | m[8][7] << 8
    for i in range(9, 15):
        bits |= m[8][14 - i] << i
    raw = bits ^ 0x5412
    data = raw >> 10
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    assert raw & 0x3FF == rem & 0x3FF, "format BCH"
    assert data >> 3 == 0, "level M"
    return data & 7


def _read(m):
    n = len(m)
    ver = (n - 17) // 4
    mask = _format_ok([[int(v) for v in row] for row in m])
    fn = qr._Matrix(ver)
    fn.draw_function_patterns()
    probe = qr._Matrix(ver)
    probe.mod = [[False] * n for _ in range(n)]
    probe.fn = fn.fn
    probe.apply_mask(mask)  # which cells the mask flips
    bits = []
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fn.fn[y][x]:
                    bits.append(int(m[y][x] ^ probe.mod[y][x]))
        right -= 2
    cws = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits) - 7, 8)]
    ec_len, groups = qr._M[ver]
    sizes = [k for c, k in groups for _ in range(c)]
    blocks = [[] for _ in sizes]
    i = 0
    for col in range(max(sizes)):
        for b, size in enumerate(sizes):
            if col < size:
                blocks[b].append(cws[i])
                i += 1
    for _ in range(ec_len):
        for b in range(len(sizes)):
            blocks[b].append(cws[i])
            i += 1
    for b in blocks:
        assert _syndromes(b, ec_len) == [0] * ec_len, "Reed-Solomon"
    data = [c for b, size in zip(blocks, sizes, strict=True) for c in b[:size]]
    bitstr = "".join(f"{c:08b}" for c in data)
    assert bitstr[:4] == "0100"
    cl = 8 if ver <= 9 else 16
    count = int(bitstr[4 : 4 + cl], 2)
    body = bitstr[4 + cl : 4 + cl + 8 * count]
    return bytes(int(body[i : i + 8], 2) for i in range(0, len(body), 8)).decode("utf-8")


@pytest.mark.parametrize("text", TEXTS)
def test_every_mask_reads_back(text):
    for mask in range(8):
        assert _read(qr.matrix(text, mask)) == text
    assert _read(qr.matrix(text)) == text  # the mask chosen by penalty


def test_fixed_patterns_match_an_independent_library():
    segno = pytest.importorskip("segno")
    for text in TEXTS:
        m = qr.matrix(text, 3)
        ver = (len(m) - 17) // 4
        ref = segno.make_qr(text, version=ver, error="m", mask=3, mode="byte", boost_error=False).matrix
        fn = qr._Matrix(ver)
        fn.draw_function_patterns()
        for y in range(len(m)):
            for x in range(len(m)):
                if fn.fn[y][x]:
                    assert m[y][x] == bool(ref[y][x]), (ver, x, y)


def test_too_long_and_the_svg_endpoint(client):
    with pytest.raises(qr.QRError):
        qr.matrix("z" * 700)
    b = business(client)
    u = base(b)
    r = client.post(
        f"{u}/qr", json={"text": "https://example.org/join?t=abc", "label": "Softphone"}, headers=b["agent"]["h"]
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.startswith("<svg") and 'aria-label="Softphone"' in r.text and "<script" not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert client.post(f"{u}/qr", json={"text": "x"}).status_code == 401
    other = business(client, "Other Co")
    assert client.post(f"{u}/qr", json={"text": "x"}, headers=other["agent"]["h"]).status_code == 403
    assert client.post(f"{u}/qr", json={"text": "q" * 601}, headers=b["agent"]["h"]).status_code == 422
    # The label is escaped.
    r = client.post(f"{u}/qr", json={"text": "x", "label": '"><script>'}, headers=b["agent"]["h"])
    assert "<script>" not in r.text
