"""A real call through the phone system (lab check, lab/ci/checks/voice-sbc.sh).

Two browser-phone sign-ins over Verto: the admin's test phone (deploy/voice/test_phone.py)
calls a "Lab call check" extension in the demo business, which rings, answers and is hung
up on. Checks sign-in, ringing a browser phone, answer and hang-up both ways, and that an
extension added in the portal reaches FreeSWITCH without a restart. Audio is not checked
here (it needs a browser); docs/commai/voice-uat.md has the browser test.

Run inside the controller container: python - [ws://freeswitch:8081] < call_check.py
Prints one line per step; exits non-zero on the first failure. Never prints a password.
"""

import asyncio
import json
import sys
import uuid

import websockets
from exaconnect_controller import db, seed
from exaconnect_controller.commai.voice import config, freeswitch
from exaconnect_controller.commai.voice.common import domain
from exaconnect_controller.settings import get_settings

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://freeswitch:8081"
NAME = "Lab call check"
LAB = """SELECT extension, sip_password FROM voice_users
         WHERE customer_id = %s AND name = %s AND status = 'active'"""
# A minimal audio offer/answer: enough for FreeSWITCH to set up and bridge the call.
SDP = (
    "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\n"
    "c=IN IP4 0.0.0.0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
)


def fail(msg: str) -> None:
    print(f"FAIL {msg}")
    sys.exit(1)


def setup() -> tuple[str, dict, dict]:
    """The two extensions and their sign-ins; adds the lab extension once."""
    settings = get_settings()
    db.init(settings.database_url)
    with db.tx() as conn:
        biz = conn.execute("SELECT id FROM customers WHERE name = %s", (seed.CUSTOMER,)).fetchone()
        admin = conn.execute(
            """SELECT v.extension, v.sip_password FROM voice_users v JOIN users u ON u.id = v.user_id
               WHERE v.customer_id = %s AND lower(u.email) = lower(%s) AND v.status = 'active'""",
            (biz["id"] if biz else None, settings.admin_email),
        ).fetchone()
        if biz is None or admin is None:
            fail("no demo business or admin test phone (deploy/voice/test_phone.py)")
        cid = biz["id"]
        lab = conn.execute(LAB, (cid, NAME)).fetchone()
        if lab is None:
            site = conn.execute(
                """SELECT name FROM voice_sites WHERE customer_id = %s
                   ORDER BY emergency_status = 'registered' DESC, created_at LIMIT 1""",
                (cid,),
            ).fetchone()
            ops = [{"op": "add_user", "name": NAME, **({"site": site["name"]} if site else {})}]
            out = config.apply(conn, cid, ops, actor="system:voice-call-check", summary=NAME, check_price=False)
            if not out["ok"]:
                fail(f"could not add the lab extension: {out['errors']}")
            freeswitch.render_business(conn, cid)
            lab = conn.execute(LAB, (cid, NAME)).fetchone()
            print(f"ok   lab extension {lab['extension']} added")
        dom = domain(cid)
    db.close()
    return dom, dict(admin), dict(lab)


class Phone:
    def __init__(self, ws, label: str):
        self.ws, self.label, self.n, self.sess = ws, label, 0, str(uuid.uuid4())
        self.events: asyncio.Queue = asyncio.Queue()
        self.replies: dict[int, asyncio.Future] = {}
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "method" in msg:
                if "id" in msg:
                    await self.ws.send(
                        json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"method": msg["method"]}})
                    )
                await self.events.put(msg)
            elif msg.get("id") in self.replies:
                self.replies.pop(msg["id"]).set_result(msg)

    async def rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.replies[self.n] = fut
        await self.ws.send(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": {**params, "sessid": self.sess}, "id": self.n})
        )
        return await asyncio.wait_for(fut, 10)

    async def expect(self, method: str, secs: float = 15) -> dict:
        end = asyncio.get_running_loop().time() + secs
        while True:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                fail(f"{self.label}: no {method} within {secs:.0f} s")
            msg = await asyncio.wait_for(self.events.get(), left)
            if msg["method"] == method:
                return msg["params"]


async def login(label: str, ext: str, pw: str, dom: str, tries: int = 1):
    for i in range(tries):
        ws = await websockets.connect(URL)
        p = Phone(ws, label)
        r = await p.rpc("login", {"login": f"{ext}@{dom}", "passwd": pw})
        if "error" not in r:
            return p
        await ws.close()
        if i + 1 < tries:
            await asyncio.sleep(2)
    fail(f"{label}: sign-in refused: {r['error'].get('message')}")


async def main():
    dom, a_user, b_user = setup()
    # A new extension appears once the watcher has reloaded the rendered config.
    b = await login("lab extension", b_user["extension"], b_user["sip_password"], dom, tries=15)
    a = await login("admin test phone", a_user["extension"], a_user["sip_password"], dom)
    print(f"ok   both browser phones signed in ({a_user['extension']} and {b_user['extension']})")
    call = str(uuid.uuid4())
    dialog = {"callID": call, "destination_number": b_user["extension"], "caller_id_number": a_user["extension"]}
    r = await a.rpc("verto.invite", {"sdp": SDP, "dialogParams": dialog})
    if "error" in r:
        fail(f"call refused: {r['error'].get('message')}")
    inv = await b.expect("verto.invite")
    print(f"ok   {b_user['extension']} rings, caller {inv.get('caller_id_number')}")
    bcall = inv["callID"]
    r = await b.rpc("verto.answer", {"sdp": SDP, "dialogParams": {"callID": bcall}})
    if "error" in r:
        fail(f"answer refused: {r['error'].get('message')}")
    await a.expect("verto.answer")
    print("ok   answered, caller connected")
    await a.rpc("verto.bye", {"dialogParams": {"callID": call}})
    await b.expect("verto.bye")
    print("ok   hang-up reached the other side")
    # Nothing rings again after a hang-up.
    try:
        msg = await asyncio.wait_for(b.events.get(), 3)
        if msg["method"] == "verto.invite":
            fail("the lab extension rang again after the hang-up")
    except TimeoutError:
        pass
    for p in (a, b):
        await p.ws.close()
    print("ok   call check passed")


asyncio.run(main())
