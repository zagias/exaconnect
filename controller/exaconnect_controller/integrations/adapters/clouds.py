"""Cloud on-ramps: one adapter interface, four providers (ADR 0026).

Private, dedicated connectivity to a cloud, ordered by ExaCarib as the
connectivity partner, as opposed to the IPsec VPN circuits of fabric.py:

- AWS Direct Connect hosted connections (AllocateHostedConnection on
  ExaCarib's interconnect, accepted by the customer's account),
- Azure ExpressRoute (the customer's circuit, read through ARM by its
  resource id and service key),
- Google Cloud Partner Interconnect (a PARTNER_PROVIDER attachment made
  from the customer's pairing key),
- Megaport (a VXC from ExaCarib's port to a cloud or partner port).

Every adapter is simulated until EXA_INTEGRATIONS_LIVE=1 and its own
credentials are set in the environment; the simulated stand-in answers in
the provider's shape so the flow, the statuses and the portal are the same.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.parse
from typing import Any

from ..transport import Http, Transport, live_enabled, sim_ok


class OnrampError(ValueError):
    pass


STATUSES = ("ordering", "pending", "available", "failed", "deleting", "deleted")


class Adapter:
    key = ""
    name = ""
    docs = ""
    api = ""
    env: tuple[str, ...] = ()
    live_needs = ""
    speeds: tuple[int, ...] = (50, 100, 200, 300, 400, 500, 1000, 2000, 5000, 10000)
    fields: tuple[tuple[str, str], ...] = ()  # what the customer gives: (name, label)

    def configured(self) -> bool:
        return all(os.environ.get(e) for e in self.env)

    def live(self) -> bool:
        return live_enabled() and self.configured()

    def validate(self, req: dict) -> dict:
        mbps = int(req.get("bandwidth_mbps") or 0)
        if mbps not in self.speeds:
            raise OnrampError(f"{self.name} offers {', '.join(map(str, self.speeds))} Mbps.")
        return {"bandwidth_mbps": mbps}

    def order(self, http: Transport, onramp: dict) -> dict:
        raise NotImplementedError

    def check(self, http: Transport, onramp: dict) -> dict:
        raise NotImplementedError

    def delete(self, http: Transport, onramp: dict) -> None:
        raise NotImplementedError

    def simulate(self, o: dict, method: str, url: str, headers: dict, body: bytes) -> Http:
        return sim_ok(200, {})

    def public(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "docs": self.docs,
            "api": self.api,
            "speeds_mbps": list(self.speeds),
            "fields": [{"name": n, "label": label} for n, label in self.fields],
            "configured": self.configured(),
            "mode": "live" if self.live() else "simulated",
            "live_needs": self.live_needs,
        }


def _raise(resp: Http, who: str) -> None:
    if not resp.ok:
        raise OnrampError(f"{who} answered {resp.status}: {resp.text[:200]}")


# ---- AWS Direct Connect --------------------------------------------------------


def sigv4(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4 (https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html)."""
    now = now or dt.datetime.now(dt.UTC)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    p = urllib.parse.urlsplit(url)
    h = {**headers, "host": p.netloc, "x-amz-date": amz_date}
    names = sorted(k.lower() for k in h)
    lower = {k.lower(): str(v).strip() for k, v in h.items()}
    canonical = "\n".join(
        [
            method,
            p.path or "/",
            p.query,
            "".join(f"{n}:{lower[n]}\n" for n in names),
            ";".join(names),
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def mac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k = mac(mac(mac(mac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        **headers,
        "x-amz-date": amz_date,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, "
        f"Signature={sig}",
    }


AWS_STATE = {
    "ordering": "pending",
    "requested": "pending",
    "pending": "pending",
    "available": "available",
    "down": "available",
    "deleting": "deleting",
    "deleted": "deleted",
    "rejected": "failed",
    "unknown": "pending",
}


class AwsDirectConnect(Adapter):
    key = "aws_dx"
    name = "AWS Direct Connect (hosted connection)"
    docs = "https://docs.aws.amazon.com/directconnect/latest/APIReference/API_AllocateHostedConnection.html"
    api = "Direct Connect API (JSON 1.1, SigV4): AllocateHostedConnection, DescribeConnections, DeleteConnection"
    env = ("EXA_AWS_DX_ACCESS_KEY_ID", "EXA_AWS_DX_SECRET_ACCESS_KEY", "EXA_AWS_DX_INTERCONNECT_ID")
    live_needs = (
        "ExaCarib as an AWS Direct Connect Delivery Partner with an interconnect (or LAG) at a Direct Connect "
        "location, and an IAM user allowed to allocate hosted connections on it."
    )
    fields = (("aws_account_id", "Your AWS account ID"), ("region", "AWS Region of the location"))

    def validate(self, req: dict) -> dict:
        out = super().validate(req)
        acct = str(req.get("aws_account_id") or "").strip()
        if not re.fullmatch(r"\d{12}", acct):
            raise OnrampError("An AWS account ID is 12 digits.")
        region = str(req.get("region") or "us-east-1").strip()
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region):
            raise OnrampError("That is not an AWS Region name, like us-east-1.")
        return {**out, "aws_account_id": acct, "region": region}

    def _call(self, http: Transport, region: str, action: str, body: dict) -> dict:
        url = os.environ.get("EXA_AWS_DX_ENDPOINT") or f"https://directconnect.{region}.amazonaws.com/"
        raw = json.dumps(body).encode()
        headers = {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": f"OvertureService.{action}"}
        if http.live:
            headers = sigv4(
                "POST",
                url,
                headers,
                raw,
                os.environ.get("EXA_AWS_DX_ACCESS_KEY_ID", ""),
                os.environ.get("EXA_AWS_DX_SECRET_ACCESS_KEY", ""),
                region,
                "directconnect",
            )
        resp = http.request("POST", url, headers=headers, body=raw)
        _raise(resp, "AWS Direct Connect")
        return resp.json()

    def order(self, http: Transport, o: dict) -> dict:
        d = o["detail"]
        vlan = int(d.get("vlan") or 100 + o["id"] % 3900)
        out = self._call(
            http,
            d["region"],
            "AllocateHostedConnection",
            {
                "connectionId": os.environ.get("EXA_AWS_DX_INTERCONNECT_ID", "dxcon-simulated"),
                "ownerAccount": d["aws_account_id"],
                "bandwidth": f"{o['bandwidth_mbps']}Mbps" if o["bandwidth_mbps"] < 1000 else f"{o['bandwidth_mbps'] // 1000}Gbps",
                "connectionName": o["name"][:100],
                "vlan": vlan,
                "tags": [{"key": "exacarib:onramp", "value": str(o["id"])}],
            },
        )
        state = out.get("connectionState", "ordering")
        return {
            "external_id": out.get("connectionId", ""),
            "provider_state": state,
            "status": AWS_STATE.get(state, "pending"),
            "pairing": {"vlan": out.get("vlan", vlan), "location": out.get("location", "")},
            "next_step": "Accept the hosted connection in the AWS console (Direct Connect > Connections), then "
            "create a virtual interface on it.",
        }

    def check(self, http: Transport, o: dict) -> dict:
        out = self._call(http, o["detail"]["region"], "DescribeConnections", {"connectionId": o["external_id"]})
        conns = out.get("connections") or [{}]
        state = conns[0].get("connectionState", "unknown")
        return {"provider_state": state, "status": AWS_STATE.get(state, "pending")}

    def delete(self, http: Transport, o: dict) -> None:
        self._call(http, o["detail"]["region"], "DeleteConnection", {"connectionId": o["external_id"]})

    def simulate(self, o: dict, method: str, url: str, headers: dict, body: bytes) -> Http:
        target = headers.get("X-Amz-Target", "")
        req = json.loads(body or b"{}")
        if target.endswith("AllocateHostedConnection"):
            return sim_ok(
                200,
                {
                    "connectionId": "dxcon-" + secrets.token_hex(4),
                    "connectionName": req.get("connectionName"),
                    "connectionState": "ordering",
                    "ownerAccount": req.get("ownerAccount"),
                    "bandwidth": req.get("bandwidth"),
                    "vlan": req.get("vlan"),
                    "location": "EqMI2",
                    "partnerName": "ExaCarib",
                },
            )
        if target.endswith("DescribeConnections"):
            return sim_ok(200, {"connections": [{"connectionId": req.get("connectionId"), "connectionState": "available"}]})
        return sim_ok(200, {"connectionId": req.get("connectionId"), "connectionState": "deleted"})


# ---- Azure ExpressRoute ---------------------------------------------------------

AZ_STATE = {"NotProvisioned": "pending", "Provisioning": "pending", "Provisioned": "available", "Deprovisioning": "deleting"}
AZ_API = "2023-09-01"


class AzureExpressRoute(Adapter):
    key = "azure_er"
    name = "Azure ExpressRoute"
    docs = "https://learn.microsoft.com/en-us/rest/api/expressroute/express-route-circuits/get"
    api = "Azure Resource Manager: Microsoft.Network/expressRouteCircuits (api-version 2023-09-01)"
    env = ("EXA_AZURE_TENANT_ID", "EXA_AZURE_CLIENT_ID", "EXA_AZURE_CLIENT_SECRET")
    live_needs = (
        "ExaCarib onboarded as an ExpressRoute connectivity provider, and an Entra app the customer grants Reader "
        "on its circuit, so Connect can follow serviceProviderProvisioningState."
    )
    fields = (("circuit_id", "The circuit's resource ID"), ("service_key", "The circuit's service key"))
    speeds = (50, 100, 200, 500, 1000, 2000, 5000, 10000)

    def validate(self, req: dict) -> dict:
        out = super().validate(req)
        rid = str(req.get("circuit_id") or "").strip()
        if not re.fullmatch(
            r"/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/]+/providers/Microsoft\.Network/expressRouteCircuits/[^/]+",
            rid,
        ):
            raise OnrampError("Give the circuit's full resource ID (/subscriptions/.../expressRouteCircuits/<name>).")
        key = str(req.get("service_key") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", key):
            raise OnrampError("The service key is a GUID from the circuit's overview page.")
        return {**out, "circuit_id": rid, "service_key": key.lower()}

    def _token(self, http: Transport) -> str:
        if not http.live:
            return "simulated"
        resp = http.request(
            "POST",
            f"{os.environ.get('EXA_AZURE_LOGIN_URL', 'https://login.microsoftonline.com')}/"
            f"{os.environ.get('EXA_AZURE_TENANT_ID', '')}/oauth2/v2.0/token",
            form={
                "grant_type": "client_credentials",
                "client_id": os.environ.get("EXA_AZURE_CLIENT_ID", ""),
                "client_secret": os.environ.get("EXA_AZURE_CLIENT_SECRET", ""),
                "scope": "https://management.azure.com/.default",
            },
        )
        _raise(resp, "Microsoft sign-in")
        tok = resp.json().get("access_token", "")
        http.secret_values.append(tok)
        return tok

    def _get(self, http: Transport, rid: str) -> dict:
        base = os.environ.get("EXA_AZURE_ARM_URL", "https://management.azure.com")
        resp = http.request(
            "GET", f"{base}{rid}?api-version={AZ_API}", headers={"Authorization": f"Bearer {self._token(http)}"}
        )
        _raise(resp, "Azure Resource Manager")
        return resp.json()

    def _state(self, http: Transport, o: dict) -> dict:
        body = self._get(http, o["detail"]["circuit_id"])
        props = body.get("properties") or {}
        if str(props.get("serviceKey", "")).lower() != o["detail"]["service_key"]:
            raise OnrampError("The service key does not match that circuit.")
        state = props.get("serviceProviderProvisioningState", "NotProvisioned")
        spp = props.get("serviceProviderProperties") or {}
        return {
            "external_id": body.get("id", o["detail"]["circuit_id"]),
            "provider_state": state,
            "status": AZ_STATE.get(state, "pending"),
            "pairing": {"peering_location": spp.get("peeringLocation", ""), "service_key": o["detail"]["service_key"]},
        }

    def order(self, http: Transport, o: dict) -> dict:
        return {
            **self._state(http, o),
            "next_step": "ExaCarib provisions the circuit; then set up private peering on it in the Azure portal.",
        }

    def check(self, http: Transport, o: dict) -> dict:
        s = self._state(http, o)
        return {"provider_state": s["provider_state"], "status": s["status"]}

    def delete(self, http: Transport, o: dict) -> None:
        return None  # the customer deletes its circuit; ExaCarib deprovisions its side

    def simulate(self, o: dict, method: str, url: str, headers: dict, body: bytes) -> Http:
        rid = urllib.parse.urlsplit(url).path
        return sim_ok(
            200,
            {
                "id": rid,
                "name": rid.rsplit("/", 1)[-1],
                "type": "Microsoft.Network/expressRouteCircuits",
                "properties": {
                    "serviceKey": o["detail"].get("service_key", ""),
                    # The partner side provisions after the order: first look NotProvisioned, then Provisioned.
                    "serviceProviderProvisioningState": "Provisioned" if o.get("provider_state") else "NotProvisioned",
                    "circuitProvisioningState": "Enabled",
                    "serviceProviderProperties": {
                        "serviceProviderName": "ExaCarib",
                        "peeringLocation": "Miami",
                        "bandwidthInMbps": 50,
                    },
                },
            },
        )


# ---- Google Cloud Partner Interconnect --------------------------------------------

GCP_STATE = {
    "PENDING_PARTNER": "pending",
    "PENDING_CUSTOMER": "pending",
    "ACTIVE": "available",
    "UNPROVISIONED": "failed",
    "DEFUNCT": "failed",
}
GCP_BW = {50: "BPS_50M", 100: "BPS_100M", 200: "BPS_200M", 300: "BPS_300M", 400: "BPS_400M", 500: "BPS_500M",
          1000: "BPS_1G", 2000: "BPS_2G", 5000: "BPS_5G", 10000: "BPS_10G"}


def gcp_token(http: Transport) -> str:
    """OAuth 2.0 for a service account: a JWT signed RS256, exchanged for an access token."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    if not http.live:
        return "simulated"
    info = json.loads(os.environ.get("EXA_GCP_SERVICE_ACCOUNT_JSON", "{}"))
    now = int(time.time())

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64(
        json.dumps(
            {
                "iss": info.get("client_email", ""),
                "scope": "https://www.googleapis.com/auth/compute",
                "aud": info.get("token_uri", "https://oauth2.googleapis.com/token"),
                "iat": now,
                "exp": now + 3600,
            }
        ).encode()
    )
    key = serialization.load_pem_private_key(info.get("private_key", "").encode(), password=None)
    sig = key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    resp = http.request(
        "POST",
        info.get("token_uri", "https://oauth2.googleapis.com/token"),
        form={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{head}.{claims}.{b64(sig)}"},
    )
    _raise(resp, "Google sign-in")
    tok = resp.json().get("access_token", "")
    http.secret_values.append(tok)
    return tok


class GooglePartnerInterconnect(Adapter):
    key = "gcp_pi"
    name = "Google Cloud Partner Interconnect"
    docs = "https://cloud.google.com/compute/docs/reference/rest/v1/interconnectAttachments"
    api = "Compute Engine API: interconnectAttachments insert (PARTNER_PROVIDER, pairingKey) and get"
    env = ("EXA_GCP_SERVICE_ACCOUNT_JSON", "EXA_GCP_PROJECT", "EXA_GCP_INTERCONNECT")
    live_needs = (
        "ExaCarib as a Partner Interconnect provider with an interconnect in its own project, and a service account "
        "with compute.interconnectAttachments.* on that project."
    )
    fields = (("pairing_key", "The VLAN attachment's pairing key"), ("region", "Google Cloud region"))

    def validate(self, req: dict) -> dict:
        out = super().validate(req)
        key = str(req.get("pairing_key") or "").strip()
        parts = key.split("/")
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f-]{36}", parts[0]) or parts[2] not in ("1", "2"):
            raise OnrampError("A pairing key looks like 7e51371e-72a3-40b5-b844-2e3efefaee59/us-central1/1.")
        return {**out, "pairing_key": key, "region": parts[1]}

    def _base(self) -> str:
        root = os.environ.get("EXA_GCP_COMPUTE_URL", "https://compute.googleapis.com/compute/v1")
        return f"{root}/projects/{os.environ.get('EXA_GCP_PROJECT', 'exacarib-simulated')}"

    def order(self, http: Transport, o: dict) -> dict:
        d = o["detail"]
        name = f"exa-onramp-{o['id']}"
        body = {
            "name": name,
            "type": "PARTNER_PROVIDER",
            "pairingKey": d["pairing_key"],
            "partnerAsn": os.environ.get("EXA_GCP_PARTNER_ASN", "64512"),
            "bandwidth": GCP_BW[o["bandwidth_mbps"]],
            "interconnect": f"{self._base()}/global/interconnects/{os.environ.get('EXA_GCP_INTERCONNECT', 'sim')}",
            "partnerMetadata": {
                "partnerName": "ExaCarib",
                "interconnectName": os.environ.get("EXA_GCP_INTERCONNECT_LABEL", "ExaCarib Miami"),
                "portalUrl": "https://www.exacarib.com",
            },
        }
        resp = http.request(
            "POST",
            f"{self._base()}/regions/{d['region']}/interconnectAttachments",
            headers={"Authorization": f"Bearer {gcp_token(http)}"},
            json_body=body,
        )
        _raise(resp, "Google Cloud")
        return {
            "external_id": name,
            "provider_state": "PENDING_CUSTOMER",
            "status": "pending",
            "pairing": {"pairing_key": d["pairing_key"], "operation": resp.json().get("name", "")},
            "next_step": "Activate the VLAN attachment in the Google Cloud console, then configure BGP on Cloud Router.",
        }

    def check(self, http: Transport, o: dict) -> dict:
        resp = http.request(
            "GET",
            f"{self._base()}/regions/{o['detail']['region']}/interconnectAttachments/{o['external_id']}",
            headers={"Authorization": f"Bearer {gcp_token(http)}"},
        )
        _raise(resp, "Google Cloud")
        state = resp.json().get("state", "PENDING_CUSTOMER")
        return {"provider_state": state, "status": GCP_STATE.get(state, "pending")}

    def delete(self, http: Transport, o: dict) -> None:
        resp = http.request(
            "DELETE",
            f"{self._base()}/regions/{o['detail']['region']}/interconnectAttachments/{o['external_id']}",
            headers={"Authorization": f"Bearer {gcp_token(http)}"},
        )
        _raise(resp, "Google Cloud")

    def simulate(self, o: dict, method: str, url: str, headers: dict, body: bytes) -> Http:
        if method == "GET":
            return sim_ok(200, {"name": url.rsplit("/", 1)[-1], "type": "PARTNER_PROVIDER", "state": "ACTIVE"})
        return sim_ok(200, {"kind": "compute#operation", "name": "operation-sim", "status": "RUNNING"})


# ---- Megaport ---------------------------------------------------------------------

MP_STATE = {"NEW": "ordering", "DEPLOYABLE": "pending", "CONFIGURED": "pending", "LIVE": "available",
            "CANCELLED": "deleted", "DECOMMISSIONED": "deleted"}


class Megaport(Adapter):
    key = "megaport"
    name = "Megaport"
    docs = "https://dev.megaport.com/"
    api = "Megaport API v3: OAuth client credentials, POST /v3/networkdesign/buy (VXC), GET /v2/product/{uid}"
    env = ("EXA_MEGAPORT_CLIENT_ID", "EXA_MEGAPORT_CLIENT_SECRET", "EXA_MEGAPORT_PORT_UID")
    live_needs = "A Megaport account with ExaCarib's port, and an API key (client ID and secret)."
    fields = (("b_end_product_uid", "The cloud or partner port's product UID"), ("service_key", "Service or pairing key, if the cloud needs one"))
    speeds = (50, 100, 200, 500, 1000, 2000, 5000, 10000)

    def validate(self, req: dict) -> dict:
        out = super().validate(req)
        uid = str(req.get("b_end_product_uid") or "").strip()
        if not re.fullmatch(r"[0-9a-f-]{36}", uid):
            raise OnrampError("Give the B-end port's product UID (a GUID from the Megaport portal).")
        return {**out, "b_end_product_uid": uid, "service_key": str(req.get("service_key") or "").strip()[:100]}

    def _base(self) -> str:
        return os.environ.get("EXA_MEGAPORT_API_URL", "https://api.megaport.com")

    def _token(self, http: Transport) -> str:
        if not http.live:
            return "simulated"
        raw = f"{os.environ.get('EXA_MEGAPORT_CLIENT_ID', '')}:{os.environ.get('EXA_MEGAPORT_CLIENT_SECRET', '')}"
        resp = http.request(
            "POST",
            os.environ.get("EXA_MEGAPORT_AUTH_URL", "https://auth-m2m.megaport.com/oauth2/token"),
            headers={"Authorization": "Basic " + base64.b64encode(raw.encode()).decode()},
            form={"grant_type": "client_credentials"},
        )
        _raise(resp, "Megaport sign-in")
        tok = resp.json().get("access_token", "")
        http.secret_values.append(tok)
        return tok

    def order(self, http: Transport, o: dict) -> dict:
        d = o["detail"]
        b_end: dict[str, Any] = {"productUid": d["b_end_product_uid"]}
        if d.get("service_key"):
            b_end["partnerConfig"] = {"connectType": "PAIRING", "pairingKey": d["service_key"]}
        body = [
            {
                "productUid": os.environ.get("EXA_MEGAPORT_PORT_UID", "00000000-0000-0000-0000-000000000000"),
                "associatedVxcs": [
                    {
                        "productName": o["name"][:60],
                        "rateLimit": o["bandwidth_mbps"],
                        "aEnd": {"vlan": 100 + o["id"] % 3900},
                        "bEnd": b_end,
                    }
                ],
            }
        ]
        resp = http.request(
            "POST",
            f"{self._base()}/v3/networkdesign/buy",
            headers={"Authorization": f"Bearer {self._token(http)}"},
            json_body=body,
        )
        _raise(resp, "Megaport")
        data = (resp.json().get("data") or [{}])[0]
        uid = data.get("technicalServiceUid") or data.get("vxcJTechnicalServiceUid") or ""
        return {
            "external_id": uid,
            "provider_state": data.get("provisioningStatus", "DEPLOYABLE"),
            "status": MP_STATE.get(data.get("provisioningStatus", "DEPLOYABLE"), "pending"),
            "pairing": {"a_end_vlan": 100 + o["id"] % 3900},
            "next_step": "Megaport builds the VXC; accept it on the cloud side if the cloud asks.",
        }

    def check(self, http: Transport, o: dict) -> dict:
        resp = http.request(
            "GET",
            f"{self._base()}/v2/product/{o['external_id']}",
            headers={"Authorization": f"Bearer {self._token(http)}"},
        )
        _raise(resp, "Megaport")
        state = (resp.json().get("data") or {}).get("provisioningStatus", "CONFIGURED")
        return {"provider_state": state, "status": MP_STATE.get(state, "pending")}

    def delete(self, http: Transport, o: dict) -> None:
        resp = http.request(
            "POST",
            f"{self._base()}/v3/product/{o['external_id']}/action/CANCEL_NOW",
            headers={"Authorization": f"Bearer {self._token(http)}"},
        )
        _raise(resp, "Megaport")

    def simulate(self, o: dict, method: str, url: str, headers: dict, body: bytes) -> Http:
        if url.endswith("/networkdesign/buy"):
            return sim_ok(
                200,
                {"message": "VXC ordered", "data": [{"technicalServiceUid": str(_uuid()), "provisioningStatus": "DEPLOYABLE"}]},
            )
        if method == "GET":
            return sim_ok(200, {"data": {"productUid": url.rsplit("/", 1)[-1], "provisioningStatus": "LIVE"}})
        return sim_ok(200, {"message": "Cancelled"})


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


ADAPTERS: dict[str, Adapter] = {
    a.key: a for a in (AwsDirectConnect(), AzureExpressRoute(), GooglePartnerInterconnect(), Megaport())
}
