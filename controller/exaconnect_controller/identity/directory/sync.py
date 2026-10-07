"""Pull sync and group presets (ADR 0030).

A pull sync turns a directory `Snapshot` into the same accounts, groups and
rights SCIM makes, through `identity.scim`, so every rule holds: people get
member rights, groups become teams, admin rights wait for a second approval,
and someone switched off or gone from the directory is switched off at once,
with every session ended and every API key revoked.

The sync is a durable job ("directory.sync") that schedules its next run.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ...commai import jobs
from ...commai.automation import vault
from .. import scim
from . import sources, templates

log = logging.getLogger("exaconnect.identity.directory")

# Refuse a sync that would switch off more than this share of the people it made
# (a wrong filter or a broken connector, not a real departure).
MASS_DISABLE_SHARE = 0.5
MASS_DISABLE_MIN = 5


class SyncError(Exception):
    """The sync can't run or was refused. The message is safe to show."""


# ---- presets ------------------------------------------------------------------------------


def apply_presets(conn: psycopg.Connection, setup: dict, only_group: Any = None) -> list[str]:
    """Apply the presets the business admin accepted to matching groups. An
    admin preset only asks for admin rights: a different admin still approves."""
    t = templates.get(setup["provider"])
    accepted = {p.group.lower(): p for p in t.presets if p.id in (setup["accepted_presets"] or [])}
    if not accepted:
        return []
    done = []
    rows = conn.execute(
        "SELECT * FROM scim_groups WHERE customer_id = %s AND (%s::uuid IS NULL OR id = %s::uuid)",
        (setup["customer_id"], only_group, only_group),
    ).fetchall()
    for g in rows:
        p = accepted.get(g["display_name"].lower())
        if p is None:
            continue
        ask_admin = p.business_admin and not g["admin_requested"]
        conn.execute(
            """UPDATE scim_groups SET seat = %s,
                 admin_requested = admin_requested OR %s,
                 admin_requested_by = CASE WHEN %s THEN %s ELSE admin_requested_by END,
                 updated_at = now() WHERE id = %s""",
            (p.seat, p.business_admin, ask_admin, setup["presets_by"], g["id"]),
        )
        for m in conn.execute("SELECT user_id FROM scim_group_members WHERE group_id = %s", (g["id"],)).fetchall():
            scim.refresh_rights(conn, setup["customer_id"], m["user_id"])
        done.append(g["display_name"])
    return done


def preview(conn: psycopg.Connection, setup: dict | None, provider: str, customer_id: Any, names: list[str]) -> list:
    """How each group would map: team, seat, admin rights (always 'needs approval' until approved)."""
    t = templates.get(provider)
    accepted = set((setup or {}).get("accepted_presets") or [])
    by_name = {p.group.lower(): p for p in t.presets}
    existing = {
        g["display_name"].lower(): g
        for g in conn.execute("SELECT * FROM scim_groups WHERE customer_id = %s", (customer_id,)).fetchall()
    }
    out = []
    for name in sorted(set(names) | {g["display_name"] for g in existing.values()}, key=str.lower):
        g = existing.get(name.lower())
        p = by_name.get(name.lower())
        admin = "none"
        if g is not None and g["admin_requested"]:
            admin = "approved" if g["admin_approved_at"] else "needs approval"
        elif p is not None and p.business_admin:
            admin = "needs approval" if p.id in accepted else "preset not accepted"
        out.append(
            {
                "group": name,
                "in_exacarib": g is not None,
                "team": name,
                "seat": (p.seat if p and p.id in accepted else g["seat"] if g else "agent"),
                "preset": p.id if p else None,
                "preset_accepted": bool(p and p.id in accepted),
                "business_admin": admin,
            }
        )
    return out


# ---- sources -------------------------------------------------------------------------------


def source_for(conn: psycopg.Connection, setup: dict):
    s = setup["settings"] or {}
    prefix = s.get("group_prefix", "ExaCarib")
    t = templates.get(setup["provider"])
    if setup["mode"] == "pull" and t.pull == "graph":
        return sources.GraphSource((setup["inputs"] or {}).get("tenant_id", ""), prefix)
    if setup["mode"] == "pull" and t.pull == "google":
        return sources.GoogleSource((setup["inputs"] or {}).get("admin_email", ""), prefix)
    if setup["mode"] == "ldap" and t.pull == "ldap":
        try:
            secret = vault.get(conn, setup["customer_id"], setup["secret_ref"]) or {}
        except vault.VaultError as e:
            raise sources.SourceError(str(e)) from None
        cfg = sources.LdapConfig(
            host=s.get("host", ""),
            base_dn=s.get("base_dn", ""),
            bind_dn=s.get("bind_dn", ""),
            flavour="ad" if t.key == "active-directory" else "openldap",
            port=int(s.get("port") or 636),
            use_ssl=not s.get("plain_lab", False),
            user_filter=s.get("user_filter", ""),
            group_filter=s.get("group_filter", ""),
            group_prefix=prefix,
            ca_cert_pem=s.get("ca_cert_pem", ""),
        )
        return sources.LdapSource(cfg, secret.get("password", ""))
    raise sources.SourceError("This set-up does not read the directory itself.")


# ---- apply a snapshot ------------------------------------------------------------------------


def _ext(setup: dict, external_id: str) -> str:
    return f"{setup['provider']}:{external_id}"[:250]


def apply(conn: psycopg.Connection, setup: dict, snap: sources.Snapshot) -> dict:
    cid = setup["customer_id"]
    summary: dict = {"added": 0, "updated": 0, "disabled": 0, "groups_added": 0, "groups_removed": 0, "skipped": []}
    known = {
        r["external_id"]: r["user_id"]
        for r in conn.execute(
            "SELECT external_id, user_id FROM directory_synced_users WHERE setup_id = %s", (setup["id"],)
        ).fetchall()
    }
    seen_active = {u.external_id for u in snap.users if u.active}
    live = {
        str(r["id"])
        for r in conn.execute(
            "SELECT id FROM users WHERE id = ANY(%s) AND disabled_at IS NULL", (list(known.values()),)
        ).fetchall()
    }
    live_known = len(live)
    going = [e for e, uid in known.items() if e not in seen_active and str(uid) in live]
    if live_known >= MASS_DISABLE_MIN and len(going) > MASS_DISABLE_SHARE * live_known:
        raise SyncError(
            f"Refused: this sync would switch off {len(going)} of {live_known} people. Check the filters and the "
            "group prefix, then run it again."
        )

    ids: dict[str, Any] = {}
    for u in snap.users:
        uid = known.get(u.external_id)
        body = {
            "userName": u.email,
            "externalId": _ext(setup, u.external_id),
            "displayName": u.display_name,
            "name": {"givenName": u.given_name, "familyName": u.family_name},
            "active": u.active,
        }
        row = conn.execute("SELECT * FROM users WHERE id = %s", (uid,)).fetchone() if uid else None
        if row is None or row["scim_deleted_at"] is not None or str(row["customer_id"]) != str(cid):
            try:
                with conn.transaction():
                    row, created = scim.create_user(conn, cid, body)
            except scim.ScimError as e:
                summary["skipped"].append(f"{u.email}: {e.detail}")
                continue
            if created:
                conn.execute("UPDATE users SET provisioned_by = 'directory' WHERE id = %s", (row["id"],))
                summary["added"] += 1
            conn.execute(
                """INSERT INTO directory_synced_users (setup_id, external_id, user_id) VALUES (%s, %s, %s)
                   ON CONFLICT (setup_id, external_id) DO UPDATE SET user_id = EXCLUDED.user_id""",
                (setup["id"], u.external_id, row["id"]),
            )
        else:
            ops = []
            if row["email"] != u.email:
                ops.append({"op": "replace", "path": "userName", "value": u.email})
            if row["given_name"] != u.given_name[:100]:
                ops.append({"op": "replace", "path": "name.givenName", "value": u.given_name})
            if row["family_name"] != u.family_name[:100]:
                ops.append({"op": "replace", "path": "name.familyName", "value": u.family_name})
            if row["display_name"] != u.display_name[:200]:
                ops.append({"op": "replace", "path": "displayName", "value": u.display_name})
            if (row["disabled_at"] is None) != u.active:
                ops.append({"op": "replace", "path": "active", "value": u.active})
                if not u.active:
                    summary["disabled"] += 1
            if ops:
                try:
                    with conn.transaction():
                        row = scim.patch_user(conn, cid, str(row["id"]), ops)
                except scim.ScimError as e:
                    summary["skipped"].append(f"{u.email}: {e.detail}")
                    continue
                if any(o["path"] != "active" for o in ops):
                    summary["updated"] += 1
        ids[u.external_id] = row["id"]

    # Gone from the directory: switched off at once, out of every synced group.
    present = {u.external_id for u in snap.users}
    for ext in known:
        if ext in present:
            continue
        row = conn.execute("SELECT * FROM users WHERE id = %s", (known[ext],)).fetchone()
        if row is None:
            continue
        if row["disabled_at"] is None:
            scim.set_active(conn, row, False)
            summary["disabled"] += 1
        conn.execute(
            """DELETE FROM scim_group_members WHERE user_id = %s AND group_id IN
               (SELECT group_id FROM directory_synced_groups WHERE setup_id = %s)""",
            (row["id"], setup["id"]),
        )
        scim.refresh_rights(conn, cid, row["id"])

    # Groups.
    gknown = {
        r["external_id"]: r["group_id"]
        for r in conn.execute(
            "SELECT external_id, group_id FROM directory_synced_groups WHERE setup_id = %s", (setup["id"],)
        ).fetchall()
    }
    for dg in snap.groups:
        gid = gknown.get(dg.external_id)
        g = conn.execute("SELECT * FROM scim_groups WHERE id = %s", (gid,)).fetchone() if gid else None
        if g is None:
            g = conn.execute(
                "SELECT * FROM scim_groups WHERE customer_id = %s AND (external_id = %s OR"
                " (display_name = %s AND external_id IS NULL))",
                (cid, _ext(setup, dg.external_id), dg.name[:120]),
            ).fetchone()
            if g is None:
                name = dg.name[:120]
                if conn.execute(
                    "SELECT 1 FROM scim_groups WHERE customer_id = %s AND display_name = %s", (cid, name)
                ).fetchone():
                    name = f"{dg.name[:100]} ({templates.get(setup['provider']).name[:16]})"
                g = scim.create_group(conn, cid, {"displayName": name, "externalId": _ext(setup, dg.external_id)})
                summary["groups_added"] += 1
            conn.execute(
                """INSERT INTO directory_synced_groups (setup_id, external_id, group_id) VALUES (%s, %s, %s)
                   ON CONFLICT (setup_id, external_id) DO UPDATE SET group_id = EXCLUDED.group_id""",
                (setup["id"], dg.external_id, g["id"]),
            )
            apply_presets(conn, setup, g["id"])
            g = conn.execute("SELECT * FROM scim_groups WHERE id = %s", (g["id"],)).fetchone()
        elif (
            g["display_name"] != dg.name[:120]
            and not conn.execute(
                "SELECT 1 FROM scim_groups WHERE customer_id = %s AND display_name = %s", (cid, dg.name[:120])
            ).fetchone()
        ):
            g = scim.patch_group(conn, cid, g, [{"op": "replace", "path": "displayName", "value": dg.name}])
        members = [str(ids[m]) for m in dg.members if m in ids]
        active = [
            str(r["id"])
            for r in conn.execute(
                "SELECT id FROM users WHERE id::text = ANY(%s) AND scim_deleted_at IS NULL", (members,)
            ).fetchall()
        ]
        scim.set_members(conn, cid, g, active)
    present_g = {dg.external_id for dg in snap.groups}
    for ext, gid in gknown.items():
        if ext in present_g:
            continue
        g = conn.execute("SELECT * FROM scim_groups WHERE id = %s", (gid,)).fetchone()
        if g is not None:
            scim.delete_group(conn, cid, g)
            summary["groups_removed"] += 1
    return summary


def run(conn: psycopg.Connection, setup: dict, actor: str = "directory-sync") -> dict:
    """Read the directory and apply it. Records the outcome on the set-up; raises SyncError on failure."""
    from ... import audit

    at = dt.datetime.now(dt.UTC).isoformat()
    try:
        snap = source_for(conn, setup).snapshot()
        with conn.transaction():
            summary = apply(conn, setup, snap)
    except (sources.SourceError, SyncError) as e:
        out = {"ok": False, "at": at, "message": str(e)}
        conn.execute(
            "UPDATE directory_setups SET last_sync = %s, synced_at = now() WHERE id = %s", (Jsonb(out), setup["id"])
        )
        audit.record(conn, actor, "directory.sync_failed", setup["provider"], setup["customer_id"], {"error": str(e)})
        raise SyncError(str(e)) from None
    out = {"ok": True, "at": at, **summary, "people": len(snap.users), "groups": len(snap.groups)}
    conn.execute(
        "UPDATE directory_setups SET last_sync = %s, synced_at = now() WHERE id = %s", (Jsonb(out), setup["id"])
    )
    audit.record(conn, actor, "directory.sync", setup["provider"], setup["customer_id"], out)
    return out


def schedule(conn: psycopg.Connection, setup: dict, delay_s: float = 0) -> None:
    slot = int((dt.datetime.now(dt.UTC).timestamp() + delay_s) // 60)
    jobs.enqueue(
        conn,
        "directory.sync",
        {"setup_id": str(setup["id"])},
        customer_id=setup["customer_id"],
        dedupe_key=f"directory.sync:{setup['id']}:{slot}",
        delay_s=delay_s,
        max_attempts=3,
    )


@jobs.handler("directory.sync")
def _job(conn: psycopg.Connection, job: dict) -> None:
    setup = conn.execute(
        "SELECT * FROM directory_setups WHERE id::text = %s FOR UPDATE", (job["payload"]["setup_id"],)
    ).fetchone()
    if setup is None or setup["status"] != "connected" or setup["mode"] not in ("pull", "ldap"):
        return None  # switched off or deleted: the schedule ends here
    try:
        run(conn, setup)
    except SyncError as e:
        log.warning("directory sync %s failed: %s", setup["id"], e)
    if job["payload"].get("once"):
        return None
    schedule(conn, setup, setup["sync_interval_s"])
    return None
