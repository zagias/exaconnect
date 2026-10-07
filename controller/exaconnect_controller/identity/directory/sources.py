"""Pull connectors (ADR 0030): read a business's people and groups when it
prefers pull over SCIM, or has no SCIM (Google Workspace, Active Directory,
LDAP). Every connector is read-only and returns the same `Snapshot`.

- Microsoft Graph: client credentials of ExaCarib's multi-tenant app
  (EXA_MS_GRAPH_CLIENT_ID / EXA_MS_GRAPH_CLIENT_SECRET) against the business's
  tenant after its admin consents. Application permissions User.Read.All and
  GroupMember.Read.All.
- Google Admin SDK Directory API: ExaCarib's service account
  (EXA_GOOGLE_DIRECTORY_CREDENTIALS: the JSON key, or a path to it) with
  domain-wide delegation the business grants, acting as one of its admins with
  three read-only scopes.
- LDAP / Active Directory over LDAPS (ldap3), bind DN plus a password kept in
  the vault. Plain LDAP only with an explicit lab-only setting.

The real adapters refuse to run until their settings are present. HTTP goes
through `commai.automation.http` (tests replace its transport); LDAP through
`open_ldap` (tests return an ldap3 MOCK_SYNC connection).
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ...commai.automation import http
from . import templates

MAX_PAGES = 200


class SourceError(Exception):
    """The directory could not be read. The message is safe to show."""


@dataclass
class DirUser:
    external_id: str
    email: str
    given_name: str = ""
    family_name: str = ""
    display_name: str = ""
    active: bool = True


@dataclass
class DirGroup:
    external_id: str
    name: str
    members: list[str] = field(default_factory=list)  # user external IDs


@dataclass
class Snapshot:
    users: list[DirUser]
    groups: list[DirGroup]


def _in_scope(name: str, prefix: str) -> bool:
    return not prefix or name.lower().startswith(prefix.lower())


# ---- Microsoft Graph -------------------------------------------------------------------------

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphSource:
    label = "Microsoft Graph"

    def __init__(self, tenant_id: str, group_prefix: str = "ExaCarib") -> None:
        self.client_id = os.environ.get("EXA_MS_GRAPH_CLIENT_ID", "")
        self.secret = os.environ.get("EXA_MS_GRAPH_CLIENT_SECRET", "")
        if not self.client_id or not self.secret:
            raise SourceError(
                "The Microsoft Graph connector is not set up yet: ExaCarib needs to register its Microsoft app "
                "(EXA_MS_GRAPH_CLIENT_ID and EXA_MS_GRAPH_CLIENT_SECRET)."
            )
        if not tenant_id:
            raise SourceError("Enter your Directory (tenant) ID first.")
        self.tenant_id = tenant_id
        self.prefix = group_prefix
        self._token = ""

    def _get_token(self) -> str:
        if self._token:
            return self._token
        r = _call(
            "POST",
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            form={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if r.status != 200 or not isinstance(r.body, dict) or not r.body.get("access_token"):
            code = r.body.get("error", "") if isinstance(r.body, dict) else ""
            if code in ("unauthorized_client", "invalid_client") or r.status in (400, 401):
                raise SourceError(
                    "Microsoft did not give ExaCarib a token for this tenant. Has a Global Administrator opened "
                    "the admin consent link?"
                )
            raise SourceError(f"Microsoft's sign-in answered {r.status}.")
        self._token = r.body["access_token"]
        return self._token

    def _pages(self, url: str, params: dict | None = None) -> list[dict]:
        out: list[dict] = []
        for _ in range(MAX_PAGES):
            r = _call("GET", url, token=self._get_token(), params=params)
            if r.status == 403:
                raise SourceError(
                    "Microsoft Graph refused: the app needs User.Read.All and GroupMember.Read.All with admin consent."
                )
            if r.status != 200 or not isinstance(r.body, dict):
                raise SourceError(f"Microsoft Graph answered {r.status}.")
            out.extend(r.body.get("value") or [])
            nxt = r.body.get("@odata.nextLink")
            if not nxt:
                return out
            url, params = nxt, None
        raise SourceError("Microsoft Graph returned too many pages.")

    def test(self) -> list[dict]:
        self._get_token()
        users = _call("GET", f"{GRAPH}/users", token=self._token, params={"$top": "1", "$select": "id"})
        groups = _call("GET", f"{GRAPH}/groups", token=self._token, params={"$top": "1", "$select": "id"})
        for r, what in ((users, "users"), (groups, "groups")):
            if r.status != 200:
                raise SourceError(f"Reading {what} from Microsoft Graph failed ({r.status}).")
        return [
            {"check": "Microsoft Graph", "ok": True, "detail": "Signed in to the tenant and read users and groups."}
        ]

    def snapshot(self) -> Snapshot:
        groups = [
            g
            for g in self._pages(f"{GRAPH}/groups", {"$select": "id,displayName", "$top": "999"})
            if _in_scope(g.get("displayName") or "", self.prefix)
        ]
        out_groups: list[DirGroup] = []
        wanted: set[str] = set()
        for g in groups:
            members = self._pages(
                f"{GRAPH}/groups/{g['id']}/members/microsoft.graph.user", {"$select": "id", "$top": "999"}
            )
            ids = [m["id"] for m in members if m.get("id")]
            wanted.update(ids)
            out_groups.append(DirGroup(g["id"], g.get("displayName") or g["id"], ids))
        users = []
        select = "id,userPrincipalName,mail,givenName,surname,displayName,accountEnabled"
        for u in self._pages(f"{GRAPH}/users", {"$select": select, "$top": "999"}):
            if u.get("id") not in wanted:
                continue
            email = (u.get("mail") or u.get("userPrincipalName") or "").lower()
            if "@" not in email:
                continue
            users.append(
                DirUser(
                    u["id"],
                    email,
                    u.get("givenName") or "",
                    u.get("surname") or "",
                    u.get("displayName") or "",
                    bool(u.get("accountEnabled", True)),
                )
            )
        return Snapshot(users, out_groups)


# ---- Google Admin SDK Directory API ----------------------------------------------------------

GOOGLE_DIR = "https://admin.googleapis.com/admin/directory/v1"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"


def _google_credentials() -> dict:
    raw = os.environ.get("EXA_GOOGLE_DIRECTORY_CREDENTIALS", "").strip()
    if not raw:
        raise SourceError(
            "The Google Directory connector is not set up yet: ExaCarib needs to create its service account "
            "(EXA_GOOGLE_DIRECTORY_CREDENTIALS)."
        )
    if not raw.startswith("{"):
        try:
            with open(raw, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            raise SourceError("EXA_GOOGLE_DIRECTORY_CREDENTIALS names a file that can't be read.") from None
    try:
        creds = json.loads(raw)
        assert creds["client_email"] and creds["private_key"]
    except (ValueError, KeyError, AssertionError, TypeError):
        raise SourceError("EXA_GOOGLE_DIRECTORY_CREDENTIALS is not a service account key.") from None
    return creds


def google_client_id() -> str:
    try:
        return str(_google_credentials().get("client_id") or "")
    except SourceError:
        return ""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class GoogleSource:
    label = "Google Directory API"

    def __init__(self, admin_email: str, group_prefix: str = "ExaCarib") -> None:
        self.creds = _google_credentials()
        if not admin_email:
            raise SourceError("Enter the Google admin to read the directory as.")
        self.admin = admin_email
        self.prefix = group_prefix
        self._token = ""

    def _assertion(self) -> str:
        now = int(time.time())
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = {
            "iss": self.creds["client_email"],
            "sub": self.admin,
            "scope": " ".join(templates.GOOGLE_SCOPES),
            "aud": GOOGLE_TOKEN,
            "iat": now,
            "exp": now + 600,
        }
        body = _b64(json.dumps(claims).encode())
        key = serialization.load_pem_private_key(self.creds["private_key"].encode(), password=None)
        sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
        return f"{head}.{body}.{_b64(sig)}"

    def _get_token(self) -> str:
        if self._token:
            return self._token
        r = _call(
            "POST",
            GOOGLE_TOKEN,
            form={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self._assertion()},
        )
        if r.status != 200 or not isinstance(r.body, dict) or not r.body.get("access_token"):
            raise SourceError(
                "Google did not give ExaCarib a token. Check domain-wide delegation lists ExaCarib's client ID with "
                "the three read-only scopes, and that the admin email is a Google admin."
            )
        self._token = r.body["access_token"]
        return self._token

    def _pages(self, url: str, key: str, params: dict) -> list[dict]:
        out: list[dict] = []
        params = dict(params)
        for _ in range(MAX_PAGES):
            r = _call("GET", url, token=self._get_token(), params=params)
            if r.status == 403:
                raise SourceError("Google refused: the delegated admin can't read the directory.")
            if r.status != 200 or not isinstance(r.body, dict):
                raise SourceError(f"The Google Directory API answered {r.status}.")
            out.extend(r.body.get(key) or [])
            token = r.body.get("nextPageToken")
            if not token:
                return out
            params["pageToken"] = token
        raise SourceError("The Google Directory API returned too many pages.")

    def test(self) -> list[dict]:
        self._pages(f"{GOOGLE_DIR}/users", "users", {"customer": "my_customer", "maxResults": "1"})
        return [{"check": "Google Directory API", "ok": True, "detail": f"Read the directory as {self.admin}."}]

    def snapshot(self) -> Snapshot:
        groups = [
            g
            for g in self._pages(f"{GOOGLE_DIR}/groups", "groups", {"customer": "my_customer", "maxResults": "200"})
            if _in_scope(g.get("name") or "", self.prefix)
        ]
        out_groups = []
        wanted: set[str] = set()
        for g in groups:
            members = self._pages(
                f"{GOOGLE_DIR}/groups/{g['id']}/members",
                "members",
                {"maxResults": "200", "includeDerivedMembership": "true"},
            )
            ids = [m["id"] for m in members if m.get("type", "USER") == "USER" and m.get("id")]
            wanted.update(ids)
            out_groups.append(DirGroup(g["id"], g.get("name") or g.get("email") or g["id"], ids))
        users = []
        for u in self._pages(f"{GOOGLE_DIR}/users", "users", {"customer": "my_customer", "maxResults": "500"}):
            if u.get("id") not in wanted or "@" not in (u.get("primaryEmail") or ""):
                continue
            name = u.get("name") or {}
            users.append(
                DirUser(
                    u["id"],
                    u["primaryEmail"].lower(),
                    name.get("givenName") or "",
                    name.get("familyName") or "",
                    name.get("fullName") or "",
                    not (u.get("suspended") or u.get("archived")),
                )
            )
        return Snapshot(users, out_groups)


def _call(method: str, url: str, **kw) -> http.Response:
    try:
        return http.request(method, url, **kw)
    except http.NetworkError as e:
        raise SourceError(str(e)) from None


# ---- LDAP and Active Directory ----------------------------------------------------------------

PLAIN_ENV = "EXA_DIRECTORY_LDAP_ALLOW_PLAIN"  # "lab" allows plain LDAP; never set it in production


@dataclass
class LdapConfig:
    host: str
    base_dn: str
    bind_dn: str
    flavour: str = "ad"  # 'ad' or 'openldap'
    port: int = 636
    use_ssl: bool = True
    user_filter: str = ""
    group_filter: str = ""
    group_prefix: str = "ExaCarib"
    ca_cert_pem: str = ""

    def users_filter(self) -> str:
        if self.user_filter:
            return self.user_filter
        if self.flavour == "ad":
            return "(&(objectCategory=person)(objectClass=user)(|(mail=*)(userPrincipalName=*)))"
        return "(&(objectClass=inetOrgPerson)(mail=*))"

    def groups_filter(self) -> str:
        if self.group_filter:
            return self.group_filter
        if self.flavour == "ad":
            return "(objectClass=group)"
        return "(|(objectClass=groupOfNames)(objectClass=groupOfUniqueNames))"


def plain_allowed() -> bool:
    return os.environ.get(PLAIN_ENV, "") == "lab"


def open_ldap(cfg: LdapConfig, password: str):
    """An unbound, read-only ldap3 connection over LDAPS. Tests replace this."""
    import ldap3

    if not cfg.use_ssl and not plain_allowed():
        raise SourceError("Plain LDAP is refused. Use LDAPS (port 636).")
    tls = None
    if cfg.use_ssl:
        tls = ldap3.Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_data=cfg.ca_cert_pem or None,
        )
    server = ldap3.Server(
        cfg.host, port=cfg.port, use_ssl=cfg.use_ssl, tls=tls, get_info=ldap3.NONE, connect_timeout=10
    )
    return ldap3.Connection(
        server, user=cfg.bind_dn, password=password, read_only=True, receive_timeout=30, raise_exceptions=False
    )


def _first(attrs: dict, name: str) -> Any:
    v = attrs.get(name)
    if isinstance(v, list):
        return v[0] if v else None
    return v


class LdapSource:
    label = "LDAP"

    def __init__(self, cfg: LdapConfig, password: str) -> None:
        if not cfg.use_ssl and not plain_allowed():
            raise SourceError("Plain LDAP is refused. Use LDAPS (port 636).")
        if not password:
            raise SourceError("Enter the bind password.")
        self.cfg = cfg
        self.password = password
        self._conn = None

    def _bind(self):
        if self._conn is not None:
            return self._conn
        try:
            c = open_ldap(self.cfg, self.password)
            ok = c.bind()
        except SourceError:
            raise
        except Exception as e:  # noqa: BLE001 - ldap3 raises many socket/TLS types
            raise SourceError(f"{self.cfg.host} could not be reached over LDAPS ({type(e).__name__}).") from None
        if not ok:
            desc = (c.result or {}).get("description", "")
            if desc == "invalidCredentials":
                raise SourceError("The directory refused the bind DN or password.")
            raise SourceError(f"The directory refused the bind ({desc or 'no reason given'}).")
        self._conn = c
        return c

    def _search(self, flt: str, attrs: list[str]) -> list[dict]:
        import ldap3

        c = self._bind()
        out: list[dict] = []
        cookie = None
        for _ in range(MAX_PAGES):
            c.search(self.cfg.base_dn, flt, ldap3.SUBTREE, attributes=attrs, paged_size=500, paged_cookie=cookie)
            if c.result.get("result") not in (0, None):
                raise SourceError(f"The directory search failed ({c.result.get('description')}).")
            out.extend(e for e in c.response if e.get("type", "searchResEntry") == "searchResEntry")
            ctrl = (c.result.get("controls") or {}).get("1.2.840.113556.1.4.319") or {}
            cookie = (ctrl.get("value") or {}).get("cookie")
            if not cookie:
                return out
        raise SourceError("The directory returned too many pages.")

    def test(self) -> list[dict]:
        self._bind()
        n = len(self._search(self.cfg.users_filter(), ["cn"]))
        return [
            {
                "check": "LDAP bind",
                "ok": True,
                "detail": f"Bound to {self.cfg.host} as {self.cfg.bind_dn} over "
                + ("LDAPS." if self.cfg.use_ssl else "plain LDAP (lab only)."),
            },
            {
                "check": "LDAP search",
                "ok": n > 0,
                "detail": f"{n} people match the user filter."
                if n
                else "No one matches the user filter under the base DN.",
            },
        ]

    def _user_id(self, e: dict) -> str:
        raw = e.get("raw_attributes") or {}
        attrs = e.get("attributes") or {}
        if self.cfg.flavour == "ad":
            guid = _first(raw, "objectGUID")
            if isinstance(guid, bytes) and len(guid) == 16:
                return str(uuid.UUID(bytes_le=guid))
        uid = _first(attrs, "entryUUID")
        return str(uid) if uid else e["dn"].lower()

    def snapshot(self) -> Snapshot:
        ad = self.cfg.flavour == "ad"
        attrs = ["mail", "givenName", "sn", "displayName", "cn"]
        attrs += (
            ["objectGUID", "userAccountControl", "userPrincipalName"]
            if ad
            else ["entryUUID", "pwdAccountLockedTime", "nsAccountLock"]
        )
        users: list[DirUser] = []
        by_dn: dict[str, str] = {}
        for e in self._search(self.cfg.users_filter(), attrs):
            a = e.get("attributes") or {}
            email = str(_first(a, "mail") or (_first(a, "userPrincipalName") if ad else "") or "").lower()
            if "@" not in email:
                continue
            if ad:
                try:
                    active = not int(_first(a, "userAccountControl") or 0) & 0x2
                except (TypeError, ValueError):
                    active = True
            else:
                lock = str(_first(a, "nsAccountLock") or "").lower()
                active = not _first(a, "pwdAccountLockedTime") and lock != "true"
            ext = self._user_id(e)
            by_dn[e["dn"].lower()] = ext
            users.append(
                DirUser(
                    ext,
                    email,
                    str(_first(a, "givenName") or ""),
                    str(_first(a, "sn") or ""),
                    str(_first(a, "displayName") or _first(a, "cn") or ""),
                    active,
                )
            )
        groups: list[DirGroup] = []
        gattrs = ["cn", "member"] + (["objectGUID"] if ad else ["entryUUID", "uniqueMember"])
        for e in self._search(self.cfg.groups_filter(), gattrs):
            a = e.get("attributes") or {}
            name = str(_first(a, "cn") or "")
            if not name or not _in_scope(name, self.cfg.group_prefix):
                continue
            dns = list(a.get("member") or []) + list(a.get("uniqueMember") or [])
            members = [by_dn[d.lower()] for d in dns if d.lower() in by_dn]
            groups.append(DirGroup(self._user_id(e), name, members))
        return Snapshot(users, groups)
