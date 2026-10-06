"""Enterprise identity providers in the sign-in gateway.

`KeycloakAdmin` is the interface; `RestKeycloakAdmin` talks to Keycloak's
admin REST API with a service-account client (EXA_KEYCLOAK_ADMIN_CLIENT_ID /
_SECRET, realm taken from EXA_OIDC_ISSUER). `SimulatedKeycloakAdmin` keeps the
same records in memory and is used whenever those settings are missing (tests,
demos). The REST client is written from Keycloak's published admin API and has
not been run against a live Keycloak yet."""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol


class GatewayError(Exception):
    """The gateway refused or could not be reached. The message is safe to show."""


@dataclass
class IdpConfig:
    alias: str
    display_name: str
    protocol: str  # 'saml' | 'oidc'
    metadata_xml: str = ""
    metadata_url: str = ""  # SAML metadata URL or OIDC discovery URL
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    enabled: bool = False


class KeycloakAdmin(Protocol):
    simulated: bool

    def upsert_idp(self, cfg: IdpConfig) -> None: ...

    def set_enabled(self, alias: str, enabled: bool) -> None: ...

    def delete_idp(self, alias: str) -> None: ...


class SimulatedKeycloakAdmin:
    """Keeps identity providers in memory. Nothing leaves the controller."""

    simulated = True

    def __init__(self) -> None:
        self.idps: dict[str, dict] = {}
        self._lock = threading.Lock()

    def upsert_idp(self, cfg: IdpConfig) -> None:
        with self._lock:
            self.idps[cfg.alias] = {
                "alias": cfg.alias,
                "displayName": cfg.display_name,
                "providerId": cfg.protocol,
                "enabled": cfg.enabled,
                "has_secret": bool(cfg.client_secret) or self.idps.get(cfg.alias, {}).get("has_secret", False),
            }

    def set_enabled(self, alias: str, enabled: bool) -> None:
        with self._lock:
            if alias in self.idps:
                self.idps[alias]["enabled"] = enabled

    def delete_idp(self, alias: str) -> None:
        with self._lock:
            self.idps.pop(alias, None)


class RestKeycloakAdmin:
    """Keycloak admin REST API (identity-provider/import-config and /instances)."""

    simulated = False

    def __init__(self, issuer: str, client_id: str, client_secret: str) -> None:
        if "/realms/" not in issuer:
            raise GatewayError("EXA_OIDC_ISSUER must look like https://<host>/realms/<realm>.")
        self.base, self.realm = issuer.rsplit("/realms/", 1)
        self.client_id, self._secret = client_id, client_secret

    def _token(self) -> str:
        data = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self._secret}
        ).encode()
        out = self._call(
            "POST",
            f"{self.base}/realms/{self.realm}/protocol/openid-connect/token",
            data,
            auth=False,
            content_type="application/x-www-form-urlencoded",
        )
        return out["access_token"]

    def _call(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        *,
        auth: bool = True,
        content_type: str = "application/json",
    ) -> dict:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type
        if auth:
            headers["Authorization"] = f"Bearer {self._token()}"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 (configured gateway)
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404 and method in ("GET", "PUT", "DELETE"):
                raise GatewayError("not found") from e
            raise GatewayError(f"The sign-in gateway answered {e.code}.") from e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise GatewayError("The sign-in gateway could not be reached.") from e

    def _admin(self, path: str) -> str:
        return f"{self.base}/admin/realms/{self.realm}{path}"

    def _import(self, cfg: IdpConfig) -> dict:
        if cfg.metadata_url:
            body = json.dumps({"providerId": cfg.protocol, "fromUrl": cfg.metadata_url}).encode()
            return self._call("POST", self._admin("/identity-provider/import-config"), body)
        # Upload the SAML metadata as a file (multipart), as the admin console does.
        boundary = "exa" + secrets.token_hex(12)
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="providerId"\r\n\r\n{cfg.protocol}\r\n',
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="metadata.xml"\r\n'
            f"Content-Type: text/xml\r\n\r\n{cfg.metadata_xml}\r\n",
            f"--{boundary}--\r\n",
        ]
        return self._call(
            "POST",
            self._admin("/identity-provider/import-config"),
            "".join(parts).encode(),
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def upsert_idp(self, cfg: IdpConfig) -> None:
        config = {k: str(v) for k, v in self._import(cfg).items()}
        if cfg.protocol == "oidc":
            config.update(
                {
                    "clientId": cfg.client_id,
                    "clientAuthMethod": "client_secret_post",
                    "defaultScope": "openid email profile",
                    "syncMode": "IMPORT",
                    "validateSignature": "true",
                    "useJwksUrl": "true",
                }
            )
            if cfg.client_secret:
                config["clientSecret"] = cfg.client_secret
        else:
            config.update(
                {
                    "syncMode": "IMPORT",
                    "principalType": "SUBJECT",
                    "nameIDPolicyFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
                }
            )
        # Reached only through kc_idp_hint from the controller, never listed on the gateway's page.
        config["hideOnLoginPage"] = "true"
        rep = {
            "alias": cfg.alias,
            "displayName": cfg.display_name,
            "providerId": cfg.protocol,
            "enabled": cfg.enabled,
            "trustEmail": True,
            "storeToken": False,
            "hideOnLogin": True,
            "config": config,
        }
        body = json.dumps(rep).encode()
        try:
            self._call("PUT", self._admin(f"/identity-provider/instances/{cfg.alias}"), body)
        except GatewayError as e:
            if str(e) != "not found":
                raise
            self._call("POST", self._admin("/identity-provider/instances"), body)

    def set_enabled(self, alias: str, enabled: bool) -> None:
        rep = self._call("GET", self._admin(f"/identity-provider/instances/{alias}"))
        rep["enabled"] = enabled
        self._call("PUT", self._admin(f"/identity-provider/instances/{alias}"), json.dumps(rep).encode())

    def delete_idp(self, alias: str) -> None:
        try:
            self._call("DELETE", self._admin(f"/identity-provider/instances/{alias}"))
        except GatewayError as e:
            if str(e) != "not found":
                raise


_simulated = SimulatedKeycloakAdmin()


def admin_for(settings) -> KeycloakAdmin:
    if settings.oidc_issuer and settings.keycloak_admin_client_id and settings.keycloak_admin_client_secret:
        return RestKeycloakAdmin(
            settings.oidc_issuer, settings.keycloak_admin_client_id, settings.keycloak_admin_client_secret
        )
    return _simulated
