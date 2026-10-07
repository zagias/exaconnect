"""Directory templates and connectors (ADR 0030): guided set-ups for Microsoft
Entra ID, Google Workspace, Okta, JumpCloud, OneLogin, Ping Identity, Auth0,
on-premises Active Directory and generic LDAP, with pull connectors for
Microsoft Graph, the Google Directory API and LDAPS.

Each provider is a go-live feature ("directory-<provider>"): it starts off and
a business can connect it only once ExaCarib has switched it on (ADR 0022)."""

from ...commai import golive
from . import sync  # noqa: F401 - registers the "directory.sync" job
from .templates import TEMPLATES

for _t in TEMPLATES.values():
    _criteria = {
        "real-tenant": f"Tested against a real {_t.name} tenant: sign-in, a test sign-in round trip and the "
        "connection test all pass.",
        "removal": "Removing or switching off someone in the directory signs them out of ExaCarib at once (checked "
        "on the real tenant).",
        "admin-approval": "A group mapped to admin rights stays pending until a second admin approves it (checked "
        "on the real tenant).",
        "instructions": f"The guided steps were followed word for word in the {_t.console} and match what it shows.",
    }
    if _t.scim.supported:
        _criteria["scim"] = f"{_t.name} SCIM provisioning ran against ExaCarib: create, update, group push, deactivate."
    if _t.pull == "graph":
        _criteria["pull"] = "Microsoft Graph pull ran with ExaCarib's registered app and read-only permissions."
    elif _t.pull == "google":
        _criteria["pull"] = "Directory API pull ran with ExaCarib's service account and read-only scopes."
    elif _t.pull == "ldap":
        _criteria["pull"] = "LDAPS sync ran against a real directory with a read-only bind account and a CA-checked "
        "certificate."
    golive.declare(
        "feature",
        f"directory-{_t.key}",
        f"Directory: {_t.name}",
        _criteria,
        {"provider": _t.key, "protocols": _t.protocols, "modes": list(_t.modes)},
    )
