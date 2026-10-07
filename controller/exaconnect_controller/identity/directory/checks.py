"""Connection tests for a directory set-up (ADR 0030): fetch and parse the
provider's SAML metadata or OpenID Connect discovery document, and say
plainly what is wrong. Each check is {"check", "ok", "detail"} and, for
warnings, "warn": True.

Fetches go only to https addresses on the provider's own hosts (or, for a
business's own AD FS, a public address), are size- and time-limited, and
never follow a redirect to another host. Tests replace `fetch`.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from cryptography import x509

MAX_BYTES = 512_000
_MD = "urn:oasis:names:tc:SAML:2.0:metadata"
_DS = "http://www.w3.org/2000/09/xmldsig#"


class FetchError(Exception):
    """The address could not be read. The message is safe to show."""


def result(check: str, ok: bool, detail: str, warn: bool = False) -> dict:
    out = {"check": check, "ok": ok, "detail": detail}
    if warn:
        out["warn"] = True
    return out


def host_allowed(url: str, hosts: tuple[str, ...]) -> str | None:
    """None when the URL may be fetched; otherwise why not."""
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname:
        return "Use an https:// address."
    if u.port not in (None, 443):
        return "Use the standard https port."
    host = u.hostname.lower()
    if hosts:
        if not any(host == h or (h.startswith(".") and host.endswith(h)) for h in hosts):
            names = ", ".join(h.lstrip(".") for h in hosts)
            return f"That address is not on the provider's own domain ({names})."
        return None
    try:
        ipaddress.ip_address(host)
        return "Use the host name, not an IP address."
    except ValueError:
        pass
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return "That address is not reachable from the internet."
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise FetchError(f"The address redirects (HTTP {code}). Give the final address instead.")


def _public(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise FetchError(f"{host} could not be found in DNS.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise FetchError(f"{host} points to a private address, which ExaCarib does not fetch.")


def _fetch(url: str) -> bytes:
    host = urllib.parse.urlparse(url).hostname or ""
    _public(host)
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"Accept": "application/xml, application/json"})
    try:
        with opener.open(req, timeout=10) as r:  # noqa: S310 - https, provider host, checked above
            body = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise FetchError(f"{host} answered HTTP {e.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise FetchError(f"{host} could not be reached.") from None
    if len(body) > MAX_BYTES:
        raise FetchError("The document is larger than 500 KB.")
    return body


fetch = _fetch  # tests replace this


# ---- SAML metadata -------------------------------------------------------------------


def parse_saml(xml: str | bytes, expected_name_id: str = "") -> list[dict]:
    """Checks on an IdP metadata document. Never raises."""
    text = xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml
    out: list[dict] = []
    if not text.strip():
        return [result("metadata", False, "The metadata is empty.")]
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return [result("metadata", False, "The metadata contains a DOCTYPE, which is refused for safety.")]
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        line, col = e.position
        return [
            result(
                "metadata",
                False,
                f"The metadata is not valid XML (line {line}, column {col}). Check you copied the whole file, "
                "not an HTML sign-in page.",
            )
        ]
    ed = root if root.tag == f"{{{_MD}}}EntityDescriptor" else root.find(f"{{{_MD}}}EntityDescriptor")
    if ed is None or not ed.get("entityID"):
        return [
            result(
                "metadata", False, f"The XML is not SAML metadata: no EntityDescriptor (found <{_local(root.tag)}>)."
            )
        ]
    out.append(result("metadata", True, f"SAML metadata for {ed.get('entityID')}."))
    idp = ed.find(f"{{{_MD}}}IDPSSODescriptor")
    if idp is None:
        if ed.find(f"{{{_MD}}}SPSSODescriptor") is not None:
            out.append(
                result(
                    "identity provider",
                    False,
                    "This is service provider metadata. Upload your identity provider's metadata, not ExaCarib's.",
                )
            )
        else:
            out.append(result("identity provider", False, "The metadata has no IDPSSODescriptor."))
        return out
    sso = idp.findall(f"{{{_MD}}}SingleSignOnService")
    https = [s.get("Location", "") for s in sso if s.get("Location", "").startswith("https://")]
    if not sso:
        out.append(result("sign-in service", False, "The metadata has no SingleSignOnService."))
    elif not https:
        out.append(result("sign-in service", False, "The sign-in service address is not https."))
    else:
        bindings = sorted({_local_binding(s.get("Binding", "")) for s in sso})
        out.append(result("sign-in service", True, f"{https[0]} ({', '.join(bindings)})."))
    certs = []
    for kd in idp.findall(f"{{{_MD}}}KeyDescriptor"):
        if kd.get("use", "signing") != "signing":
            continue
        for c in kd.iter(f"{{{_DS}}}X509Certificate"):
            certs.append((c.text or "").strip())
    if not certs:
        out.append(result("signing certificate", False, "The metadata has no signing certificate."))
    else:
        out.append(_cert_check(certs))
    formats = [(n.text or "").strip() for n in idp.findall(f"{{{_MD}}}NameIDFormat")]
    if expected_name_id and formats and expected_name_id not in formats:
        out.append(
            result(
                "name ID format",
                True,
                f"The provider doesn't list {expected_name_id}; set it in the app's settings.",
                warn=True,
            )
        )
    return out


def _cert_check(certs: list[str]) -> dict:
    import base64

    soonest: dt.datetime | None = None
    for b64 in certs:
        try:
            cert = x509.load_der_x509_certificate(base64.b64decode("".join(b64.split())))
        except Exception:  # noqa: BLE001 - any decoding problem is reported the same way
            return result("signing certificate", False, "A signing certificate in the metadata can't be read.")
        end = cert.not_valid_after_utc
        soonest = end if soonest is None or end < soonest else soonest
    now = dt.datetime.now(dt.UTC)
    assert soonest is not None
    day = soonest.strftime("%d %b %Y")
    if soonest < now:
        return result("signing certificate", False, f"The signing certificate expired on {day}.")
    if soonest < now + dt.timedelta(days=30):
        return result(
            "signing certificate", True, f"The signing certificate expires on {day}. Renew it soon.", warn=True
        )
    return result("signing certificate", True, f"{len(certs)} signing certificate(s), valid until {day}.")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _local_binding(b: str) -> str:
    return b.rsplit(":", 1)[-1] or "unknown binding"


# ---- OpenID Connect discovery ----------------------------------------------------------


def parse_discovery(body: bytes | str, url: str) -> list[dict]:
    try:
        doc = json.loads(body)
    except ValueError:
        return [
            result(
                "discovery",
                False,
                "The discovery address did not return JSON. Check it ends in /.well-known/openid-configuration.",
            )
        ]
    if not isinstance(doc, dict):
        return [result("discovery", False, "The discovery document is not a JSON object.")]
    out = []
    need = ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
    gone = [k for k in need if not doc.get(k)]
    if gone:
        return [result("discovery", False, f"The discovery document is missing {', '.join(gone)}.")]
    out.append(result("discovery", True, f"OpenID Connect provider {doc['issuer']}."))
    bad = [k for k in need if not str(doc[k]).startswith("https://")]
    if bad:
        out.append(result("https", False, f"{', '.join(bad)} is not an https address."))
    expected = url.removesuffix("/.well-known/openid-configuration")
    if doc["issuer"].rstrip("/") != expected.rstrip("/") and "{tenantid}" not in doc["issuer"]:
        out.append(result("issuer", False, f"The issuer ({doc['issuer']}) does not match the discovery address."))
    algs = doc.get("id_token_signing_alg_values_supported") or []
    if algs and "RS256" not in algs:
        out.append(result("signing", False, f"The provider does not offer RS256 ID tokens (offers {', '.join(algs)})."))
    return out


def fetch_checks(kind: str, url: str, hosts: tuple[str, ...], expected_name_id: str = "") -> list[dict]:
    why = host_allowed(url, hosts)
    if why:
        return [result("address", False, why)]
    try:
        body = fetch(url)
    except FetchError as e:
        return [result("fetch", False, str(e))]
    if kind == "saml":
        return parse_saml(body, expected_name_id)
    return parse_discovery(body, url)
