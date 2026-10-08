"""Print an organisation's sites, circuits and how its traffic is encrypted.

EXACONNECT_URL=https://connect.exacarib.com EXACONNECT_API_KEY=exa_... python inventory.py
"""

from exaconnect import ExaConnect


def main() -> None:
    with ExaConnect() as exa:
        me = exa.me()
        cid = me["customer_id"] or exa.sites.list()[0]["customer_id"]
        print(f"Signed in as {me['email']} ({me['role']})")
        for s in exa.sites.by_name(cid).values():
            print(f"  site {s['name']:<12} {s['kind']:<5} {s['location']}")
        for c in exa.circuits.list(cid):
            pair = " (resilient pair)" if c.get("resilient") else ""
            print(f"  circuit {c['name']:<24} {c['status']:<12} {c['bandwidth_mbps']} Mbps{pair}")
        rep = exa.encryption.report(cid)
        print(f"  encrypted: {rep['summary']['encrypted']} of {rep['summary']['total']} paths and tunnels")


if __name__ == "__main__":
    main()
