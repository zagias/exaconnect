#!/usr/bin/env python3
"""Prepare/activate immutable local voice policy; activation is not runtime qualification."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree as XML

SCHEMA = "jibsy-protected-ingress-v1"
HEX = re.compile(r"[0-9a-f]{64}")
DID = re.compile(r"\+[1-9][0-9]{6,14}")
TENANT = re.compile(r"c[0-9a-f]{12}")
MEMBERS = (
    "policy.json", "edge.list", "peers.list", "public/routes.xml",
    "protected/routes.xml", "contexts/routes.xml", "ivr/routes.xml",
)
REMOTE = "/exacarib/voice-approved"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def read_candidate(directory: Path, expected: str) -> tuple[dict, bytes]:
    if not HEX.fullmatch(expected):
        raise ValueError("An exact reviewed policy digest is required.")
    raw = (directory / "protected-ingress.json").read_bytes()
    policy = json.loads(raw)
    if set(policy) != {"schema", "entries", "address_sha256", "revision"} or policy["schema"] != SCHEMA:
        raise ValueError("Unsupported protected ingress candidate.")
    body = {key: value for key, value in policy.items() if key != "revision"}
    if sha(canonical(body)) != expected or policy["revision"] != expected:
        raise ValueError("Policy differs from the reviewed generation.")
    if sha((directory / "address.list").read_bytes()) != policy["address_sha256"]:
        raise ValueError("Address list and policy are different generations.")
    if not isinstance(policy["entries"], list) or len(policy["entries"]) > 10000:
        raise ValueError("Invalid ingress entry list.")
    seen, groups = set(), set()
    for row in policy["entries"]:
        if (
            set(row) != {"did", "tenant", "state", "revision", "group", "sources", "carrier_set", "destination"}
            or not DID.fullmatch(row["did"]) or not TENANT.fullmatch(row["tenant"])
            or row["did"] in seen
        ):
            raise ValueError("Invalid or duplicate owned DID.")
        seen.add(row["did"])
        if row["state"] in ("blocked", "legacy"):
            want = "" if row["state"] == "blocked" else "LEGACY"
            if (
                row["revision"] != want or row["group"] != 0 or row["sources"] != []
                or row["carrier_set"] != 0 or row["destination"] != ""
            ):
                raise ValueError("Invalid blocked/legacy policy.")
        elif row["state"] == "protected":
            revision, group, sources = row["revision"], row["group"], row["sources"]
            if (
                not HEX.fullmatch(revision) or type(group) is not int
                or group != 1000 + int(revision[:7], 16) or group in groups
                or not isinstance(sources, list) or not 1 <= len(sources) <= 32
                or type(row["carrier_set"]) is not int or row["carrier_set"] < 1
                or not re.fullmatch(r"sip:(?:\[[0-9a-f:]+\]|[a-z0-9.-]+):[1-9][0-9]{0,4};transport=tls", row["destination"])
            ):
                raise ValueError("Invalid protected source group.")
            groups.add(group)
            for source in sources:
                if ipaddress.ip_network(source, strict=True).num_addresses > 256:
                    raise ValueError("Protected source group is too wide.")
        else:
            raise ValueError("Invalid ingress state.")
    return policy, raw


def read_peers(path: Path) -> dict:
    peers = json.loads(path.read_text())
    if set(peers) != {"sbc_ip", "pbx_ip"}:
        raise ValueError("Unexpected private peer metadata.")
    for value in peers.values():
        address = ipaddress.ip_address(value)
        if (
            address.version != 4 or not address.is_private or address.is_loopback
            or address.is_multicast or address.is_unspecified or address.is_link_local
        ):
            raise ValueError("Use exact private IPv4 peer identities.")
    if peers["sbc_ip"] == peers["pbx_ip"]:
        raise ValueError("Private peer identities must differ.")
    return peers


def _xml(path: Path):
    raw = path.read_bytes()
    if len(raw) > 16_000_000 or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise ValueError("Unsupported rendered XML.")
    root = XML.fromstring(raw)
    if root.tag != "include":
        raise ValueError("Rendered fragment must be an include.")
    return root


def _verify_routes(policy: dict, tenant: str, public, private):
    """Reject partial or mismatched policy/render pairs before publication."""
    expected_public, expected_private = set(), set()
    for row in policy["entries"]:
        if row["tenant"] != tenant:
            continue
        digits = row["did"][1:]
        kind = "in" if row["state"] == "legacy" else "deny"
        name = f"owned-{kind}-{digits}"
        expected_public.add(name)
        extension = public.find(f"./extension[@name='{name}']")
        if extension is None or extension.attrib != {"name": name} or len(extension) != 1:
            raise ValueError("Missing exact owned public route.")
        condition = extension.find("./condition")
        if (
            condition is None or condition.get("field") != "destination_number"
            or condition.get("expression") != r"^\+?" + digits + "$"
        ):
            raise ValueError("Owned DID route differs from policy.")
        transfer = extension.find(".//action[@application='transfer']")
        if kind == "deny":
            actions = extension.findall(".//action")
            if (
                len(condition) != 1 or len(actions) != 1 or len(actions[0]) != 0
                or actions[0].attrib != {"application": "hangup", "data": "CALL_REJECTED"}
            ):
                raise ValueError("Protected public route is not unconditional denial.")
        elif (
            transfer is None or transfer.get("data") != f"exa-in-{digits} XML {tenant}"
            or [action.get("application") for action in condition] != ["set", "export", "export", "transfer"]
            or any(action.tag != "action" or len(action) for action in condition)
        ):
            raise ValueError("Owned route targets another tenant.")
        if row["state"] == "protected":
            name = f"protected-in-{digits}"
            expected_private.add(name)
            extension = private.find(f"./extension[@name='{name}']")
            if extension is None or extension.attrib != {"name": name} or len(extension) != 2:
                raise ValueError("Missing protected private route.")
            revision = extension.find("./condition[@field='$" + "{sip_h_X-Exa-Ingress-Policy}']")
            secure = extension.find(
                ".//action[@application='set'][@data='rtp_secure_media_inbound=mandatory:AES_CM_128_HMAC_SHA1_80']"
            )
            transfer = extension.find(".//action[@application='transfer']")
            if (
                revision is None or revision.get("expression") != "^" + row["revision"] + "$"
                or secure is None or transfer is None
                or transfer.get("data") != f"exa-in-{digits} XML {tenant}"
                or extension[0].attrib != {"field": "destination_number", "expression": r"^\+?" + digits + "$"}
                or len(extension[0]) != 0 or extension[1] is not revision
                or [action.get("application") for action in revision] != ["set", "set", "export", "export", "transfer"]
                or revision[0] is not secure
                or any(action.tag != "action" or len(action) for action in revision)
            ):
                raise ValueError("Private revision, media, or tenant differs.")
    if (
        {node.get("name") for node in public} != expected_public
        or {node.get("name") for node in private} != expected_private
    ):
        raise ValueError("Unexpected or missing owned routes.")


def freeze_context(root, tenant: str, frozen: str):
    """Pin routing references; preserve signed controller tenant fields."""
    for node in root.iter():
        if node.tag == "context":
            if node.get("name") != tenant:
                raise ValueError("Unexpected tenant dialplan context.")
            node.set("name", frozen)
        for attr in ("data", "param", "name"):
            value = node.get(attr)
            if value is None:
                continue
            value = re.sub(r"( XML )" + tenant + r"(?=$|[\s,}])", r"\g<1>" + frozen, value)
            value = re.sub(r"(loopback/[^/\s]+/)" + tenant + r"(?=$|[,}\s])", r"\g<1>" + frozen, value)
            value = value.replace("force_transfer_context=" + tenant, "force_transfer_context=" + frozen)
            value = value.replace(tenant + "_menu_", frozen + "_menu_")
            node.set(attr, value)
    return root


def _bytes(root) -> bytes:
    return XML.tostring(root, encoding="utf-8", xml_declaration=False) + b"\n"


def prepare(directory: Path, fs_directory: Path, peers_path: Path, root: Path, expected: str) -> Path:
    policy, raw = read_candidate(directory, expected)
    peers = read_peers(peers_path)
    public, private, contexts, menus = (XML.Element("include") for _ in range(4))
    snapshots = {}
    for tenant in sorted({row["tenant"] for row in policy["entries"]}):
        snapshots[tenant] = {
            name: _xml(fs_directory / tenant / filename)
            for name, filename in (
                ("public", "owned-public.xml"), ("private", "protected.xml"),
                ("context", "dialplan.xml"), ("ivr", "ivr.xml"),
            )
        }
        _verify_routes(policy, tenant, snapshots[tenant]["public"], snapshots[tenant]["private"])
    seed = sha(canonical({
        "policy": policy, "peers": peers,
        "renders": {
            tenant: {name: sha(_bytes(xml)) for name, xml in parts.items()}
            for tenant, parts in snapshots.items()
        },
    }))
    for tenant, parts in snapshots.items():
        frozen = tenant + "_g" + seed[:16]
        for name, destination in (("public", public), ("private", private), ("context", contexts), ("ivr", menus)):
            destination.extend(freeze_context(parts[name], tenant, frozen))
    rows = [
        " ".join((
            row["did"], row["state"], str(row["group"]), row["revision"] or "blocked",
            ",".join(row["sources"]) or "-", str(row["carrier_set"]), row["destination"] or "-",
        )) + "\n"
        for row in policy["entries"]
    ]
    files = {
        "policy.json": raw, "edge.list": "".join(rows).encode(),
        "peers.list": (peers["sbc_ip"] + " " + peers["pbx_ip"] + "\n").encode(),
        "public/routes.xml": _bytes(public), "protected/routes.xml": _bytes(private),
        "contexts/routes.xml": _bytes(contexts), "ivr/routes.xml": _bytes(menus),
    }
    sums = "".join(sha(files[name]) + "  " + name + "\n" for name in MEMBERS).encode()
    generation = sha(sums)
    base = root / "generations"
    base.mkdir(parents=True, exist_ok=True)
    destination = base / generation
    if destination.exists():
        verify_bundle(destination, generation)
        return destination
    with tempfile.TemporaryDirectory(prefix=".ingress-", dir=base) as staging:
        stage = Path(staging)
        for name, content in {**files, "SHA256SUMS": sums}.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o444)
        for child_directory in stage.iterdir():
            if child_directory.is_dir():
                child_directory.chmod(0o755)
        stage.chmod(0o755)
        os.rename(stage, destination)
    return destination


def verify_bundle(bundle: Path, expected: str) -> dict:
    if not HEX.fullmatch(expected) or bundle.name != expected or bundle.is_symlink():
        raise ValueError("Use the exact reviewed immutable bundle directory.")
    sums = (bundle / "SHA256SUMS").read_bytes()
    if sha(sums) != expected:
        raise ValueError("Bundle digest differs from approval.")
    lines = sums.decode().splitlines()
    if len(lines) != len(MEMBERS):
        raise ValueError("Incomplete bundle hash list.")
    for name, line in zip(MEMBERS, lines, strict=True):
        value, filename = line.split(maxsplit=1)
        target = bundle / name
        if (
            filename != name or target.is_symlink()
            or any((bundle / parent).is_symlink() for parent in Path(name).parents if str(parent) != ".")
            or sha(target.read_bytes()) != value
        ):
            raise ValueError("Missing, corrupt, or unexpected bundle member.")
    return json.loads((bundle / "policy.json").read_bytes())


class Native:
    def __init__(self, container="kamailio", pbx="freeswitch"):
        for name in (container, pbx):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
                raise ValueError("Invalid local container name.")
        self.container, self.pbx = container, pbx

    def command(self, container, *args):
        result = subprocess.run(
            ["docker", "exec", container, *args], capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode:
            raise RuntimeError("Native validation/reload failed; inspect details privately.")
        return result.stdout

    def rpc(self, *args):
        return self.command(self.container, "kamcmd", *args)


def _shape(node):
    return node.tag, tuple(sorted(node.attrib.items())), (node.text or "").strip(), tuple(map(_shape, node))


def _loaded_context(native, name):
    output = native.command(native.pbx, "fs_cli", "-x", "xml_locate dialplan context name " + name)
    root = XML.fromstring(output)
    context = root if root.tag == "context" else root.find(".//context")
    if context is None or context.get("name") != name:
        raise ValueError("FreeSWITCH did not load the expected approved context.")
    return context


def operator_root(root: Path):
    """Approval storage is root-owned, outside any controller-writable parent."""
    if os.geteuid() != 0 or not root.is_absolute():
        raise PermissionError("Activation requires the private operator root and an absolute approved directory.")
    for path in (root, *root.parents):
        metadata = path.stat()
        if path.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise PermissionError("Approved storage and every parent must be root-owned and not group/world writable.")


def activate(bundle: Path, root: Path, expected: str, native: Native) -> dict:
    operator_root(root)
    with (root / ".activation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _activate(bundle, root, expected, native)


def _activate(bundle: Path, root: Path, expected: str, native: Native) -> dict:
    native.rpc("htable.sets", "protectedin", "gate", "closed")
    try:
        policy = verify_bundle(bundle, expected)
        if bundle.resolve().parent != (root / "generations").resolve():
            raise ValueError("Bundle must be inside the approved shared root.")
        native.command(native.container, "kamailio", "-c", "-f", "/etc/kamailio/kamailio.cfg")
        for container in (native.container, native.pbx):
            owner, mode = native.command(container, "stat", "-c", "%u %a", REMOTE).split()
            if owner != "0" or int(mode, 8) & 0o022:
                raise PermissionError("Runtime approved mount must be root-owned and not group/world writable.")
            for name in MEMBERS:
                mounted = f"{REMOTE}/generations/{expected}/{name}"
                output = native.command(container, "sha256sum", mounted).split()
                if not output or output[0] != sha((bundle / name).read_bytes()):
                    raise ValueError("Runtime mount differs from the approved bundle.")
        sbc_ip, pbx_ip = (bundle / "peers.list").read_text().split()
        native.rpc("htable.flush", "protectedin")
        native.rpc("htable.sets", "protectedin", "gate", "closed")
        native.rpc("htable.sets", "protectedin", "pbx_ip", pbx_ip)
        native.rpc("htable.sets", "protectedin", "sbc_ip", sbc_ip)
        native.rpc("htable.sets", "protectedin", "pbx_uri", f"sip:{pbx_ip}:5092;transport=tls")
        native.rpc("htable.sets", "protectedin", "sbc_route", f"{sbc_ip}:5091;transport=tls;r2=on")
        for row in policy["entries"]:
            value = str(row["group"]) + ":" + (row["revision"] or "blocked") + ":" + str(row["carrier_set"])
            native.rpc("htable.sets", "protectedin", expected + ":" + row["did"], value)
            if row["state"] == "protected":
                native.rpc("htable.sets", "protectedin", expected + ":acl:" + str(row["group"]), ",".join(row["sources"]))
                native.rpc("htable.sets", "protectedin", expected + ":set:" + str(row["carrier_set"]), str(row["group"]))
                native.rpc(
                    "htable.sets", "protectedin", expected + ":destination:" + str(row["carrier_set"]), row["destination"]
                )
        link = root / (".current-" + str(os.getpid()))
        link.symlink_to(Path("generations") / expected)
        os.replace(link, root / "current")
        # A file bind may retain its old inode after host replacement. Require
        # the newly switched directory indirection to be visible in BOTH peers.
        for container in (native.container, native.pbx):
            visible = native.command(container, "readlink", REMOTE + "/current").strip()
            if visible != "generations/" + expected:
                raise ValueError("Runtime does not see the newly switched approved directory.")
        output = native.command(native.pbx, "fs_cli", "-x", "reloadxml")
        if not output.strip().startswith("+OK"):
            raise RuntimeError("FreeSWITCH did not accept the approved XML generation.")
        for context in XML.parse(bundle / "contexts/routes.xml").getroot():
            if _shape(_loaded_context(native, context.get("name"))) != _shape(context):
                raise ValueError("Loaded tenant routing context differs from the approved snapshot.")
        for filename, context_name in (("public/routes.xml", "public"), ("protected/routes.xml", "exacarib-protected")):
            expected_routes = XML.parse(bundle / filename).getroot()
            if not len(expected_routes):
                continue
            loaded = _loaded_context(native, context_name)
            for extension in expected_routes:
                matches = [node for node in loaded if node.get("name") == extension.get("name")]
                if len(matches) != 1 or _shape(matches[0]) != _shape(extension):
                    raise ValueError("Loaded public/private routing differs from the approved snapshot.")
        verify_bundle(bundle, expected)
        native.rpc("htable.sets", "protectedin", "active", expected)
        receipt = root / (".active-" + str(os.getpid()))
        receipt.write_text(expected + "\n")
        os.replace(receipt, root / "active-revision")
        native.rpc("htable.sets", "protectedin", "gate", "open")
    except BaseException:
        native.rpc("htable.sets", "protectedin", "gate", "closed")
        raise
    return {"status": "activated_configuration_only", "bundle": expected, "runtime_qualified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--directory", type=Path, required=True)
    prep.add_argument("--freeswitch-directory", type=Path, required=True)
    prep.add_argument("--peers", type=Path, required=True)
    prep.add_argument("--expected-policy", required=True)
    prep.add_argument("--approved-root", type=Path, required=True)
    apply = sub.add_parser("activate")
    apply.add_argument("--bundle", type=Path, required=True)
    apply.add_argument("--approved-root", type=Path, required=True)
    apply.add_argument("--expected-bundle", required=True)
    apply.add_argument("--container", default="kamailio")
    apply.add_argument("--pbx-container", default="freeswitch")
    args = parser.parse_args()
    if args.action == "prepare":
        path = prepare(args.directory, args.freeswitch_directory, args.peers, args.approved_root, args.expected_policy)
        print(json.dumps({"status": "prepared_offline_only", "bundle": path.name, "directory": str(path)}))
    else:
        print(json.dumps(activate(
            args.bundle, args.approved_root, args.expected_bundle, Native(args.container, args.pbx_container)
        )))


if __name__ == "__main__":
    main()
