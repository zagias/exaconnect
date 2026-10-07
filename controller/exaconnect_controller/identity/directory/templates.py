"""Directory templates (ADR 0030): one guided set-up per identity provider.

A template is data. It holds what the provider needs from us (our ACS URL,
entity ID, redirect URI, SCIM address, generated per business), what we need
from the provider (its metadata URL pattern, claim and attribute names, NameID
format, signing algorithm), the SCIM attribute mappings and quirks it is known
for, the plain-English steps for its admin console, and preset group mappings
that the business admin accepts (an admin preset still waits for a second
approval, as every admin mapping does, ADR 0017).

`render` turns a template plus a business's inputs into the values to paste.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SAML_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
RSA_SHA256 = "RSA-SHA256 (http://www.w3.org/2001/04/xmldsig-more#rsa-sha256)"
_MS = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims"


@dataclass(frozen=True)
class Input:
    """Something the business types in, such as its tenant ID."""

    key: str
    label: str
    pattern: str
    example: str
    help: str = ""
    needed_for: tuple[str, ...] = ("saml", "oidc")  # which parts of the set-up use it


@dataclass(frozen=True)
class Claims:
    email: str
    name: str
    given_name: str
    family_name: str
    groups: str
    note: str = ""


@dataclass(frozen=True)
class Saml:
    metadata_url: str  # pattern with {input} placeholders; "" when the provider only offers a download
    name_id_format: str
    name_id_value: str
    signing: str
    claims: Claims
    note: str = ""


@dataclass(frozen=True)
class Oidc:
    discovery_url: str
    claims: Claims
    scopes: str = "openid email profile"
    signing: str = "RS256"
    note: str = ""


@dataclass(frozen=True)
class Scim:
    supported: bool
    mappings: tuple[tuple[str, str], ...] = ()  # (our SCIM attribute, what the provider sends)
    quirks: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Preset:
    id: str
    group: str
    seat: str  # 'agent' or 'internal'
    business_admin: bool = False
    note: str = ""


@dataclass(frozen=True)
class Step:
    when: str  # 'all', 'saml', 'oidc', 'scim', 'pull', 'ldap'
    text: str


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    console: str  # where the business admin works
    summary: str
    inputs: tuple[Input, ...]
    saml: Saml | None
    oidc: Oidc | None
    scim: Scim
    pull: str | None  # 'graph', 'google', 'ldap' or None
    labels: dict[str, str]  # our value key -> the label in the provider's console
    steps: tuple[Step, ...]
    metadata_hosts: tuple[str, ...] = ()  # allowed hosts (suffixes) for metadata and discovery URLs
    presets: tuple[Preset, ...] = ()
    default_protocol: str = "saml"
    modes: tuple[str, ...] = ("scim",)

    @property
    def protocols(self) -> list[str]:
        return [p for p, spec in (("saml", self.saml), ("oidc", self.oidc)) if spec]


_PRESETS = (
    Preset("agents", "ExaCarib Agents", "agent", note="Reply to customers in CommAI."),
    Preset("internal", "ExaCarib Internal", "internal", note="Read conversations and write private notes."),
    Preset(
        "admins",
        "ExaCarib Admins",
        "agent",
        business_admin=True,
        note="The whole organisation account. Waits for a second admin to approve it.",
    ),
)

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_HOST = r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"

_COMMON_TAIL = (
    Step("all", "Enter your email domains above. ExaCarib checks each one before it sends anyone to your sign-in."),
    Step("all", "Run the connection test. Fix anything it marks as a problem, then run a test sign-in."),
    Step("all", "Accept the group mappings you want, then choose Connect."),
)

ENTRA = Template(
    key="entra",
    name="Microsoft Entra ID",
    console="Microsoft Entra admin centre",
    summary="Microsoft 365 and Azure AD tenants. SAML or OpenID Connect sign-in; SCIM or Microsoft Graph for people.",
    inputs=(
        Input(
            "tenant_id",
            "Directory (tenant) ID",
            _GUID,
            "00000000-0000-0000-0000-000000000000",
            "Entra admin centre > Overview > Tenant ID.",
            ("saml", "oidc", "pull"),
        ),
        Input(
            "app_id",
            "Application (client) ID",
            _GUID,
            "11111111-1111-1111-1111-111111111111",
            "Shown on the enterprise application's Overview page after you create it.",
            ("saml", "oidc"),
        ),
    ),
    saml=Saml(
        metadata_url="https://login.microsoftonline.com/{tenant_id}/federationmetadata/2007-06/federationmetadata.xml?appid={app_id}",
        name_id_format=SAML_EMAIL,
        name_id_value="user.mail (or user.userprincipalname when it is the email address)",
        signing=RSA_SHA256 + "; sign the SAML response and assertion",
        claims=Claims(
            email=f"{_MS}/emailaddress",
            name=f"{_MS}/name",
            given_name=f"{_MS}/givenname",
            family_name=f"{_MS}/surname",
            groups="http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
            note="Add a group claim for 'Groups assigned to the application' and emit the group name "
            "(sAMAccountName or cloud display name), not the object ID. Entra stops sending groups past 150 "
            "in SAML; use SCIM or Microsoft Graph for people and groups.",
        ),
    ),
    oidc=Oidc(
        discovery_url="https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration",
        claims=Claims(
            "email",
            "name",
            "given_name",
            "family_name",
            "groups",
            "Add the optional claims email, given_name and family_name in Token configuration. "
            "The groups claim carries object IDs unless you choose to emit names.",
        ),
    ),
    scim=Scim(
        supported=True,
        mappings=(
            ("userName", "userPrincipalName"),
            ("active", 'Switch([IsSoftDeleted], , "False", "True", "True", "False")'),
            ("displayName", "displayName"),
            ("name.givenName", "givenName"),
            ("name.familyName", "surname"),
            ('emails[type eq "work"].value', "mail"),
            ("externalId", "mailNickname"),
            ("Group displayName", "displayName"),
            ("Group members", "members"),
        ),
        quirks=(
            "PATCH operations arrive in title case ('Replace', 'Add', 'Remove'). Handled.",
            'active arrives as the strings "True" and "False". Handled.',
            'Members are removed with a members[value eq "id"] path or a value list. Both handled.',
            'Email changes use the path emails[type eq "work"].value. Handled.',
            "Without the feature flag, older PATCH shapes are sent; add ?aadOptscim062020 to the Tenant URL.",
            "Provisioning runs about every 40 minutes. Use 'Provision on demand' to test one person.",
        ),
    ),
    pull="graph",
    labels={
        "entity_id": "Identifier (Entity ID)",
        "acs_url": "Reply URL (Assertion Consumer Service URL)",
        "sign_on_url": "Sign on URL",
        "redirect_uri": "Redirect URI (Web)",
        "scim_url": "Tenant URL",
        "scim_token": "Secret Token",
        "admin_consent_url": "Admin consent link (Microsoft Graph pull)",
    },
    steps=(
        Step(
            "saml",
            "In the Microsoft Entra admin centre, open Enterprise applications > New application > "
            "Create your own application, name it 'ExaCarib Connect' and choose 'Integrate any other "
            "application you don't find in the gallery'.",
        ),
        Step(
            "saml",
            "Open Single sign-on > SAML. In Basic SAML Configuration, paste the Identifier (Entity ID) "
            "{entity_id}, the Reply URL {acs_url} and the Sign on URL {sign_on_url}.",
        ),
        Step(
            "saml",
            "In Attributes & Claims, set the Unique User Identifier to user.mail with the Email address "
            "format, then add a group claim for groups assigned to the application.",
        ),
        Step(
            "oidc",
            "Open App registrations > New registration. Add a Web redirect URI {redirect_uri}. Under "
            "Certificates & secrets make a client secret and paste it here with the Application (client) "
            "ID. ExaCarib passes the secret to the sign-in gateway and does not keep it.",
        ),
        Step(
            "all", "Under Users and groups, assign the groups who should use ExaCarib (for example 'ExaCarib Agents')."
        ),
        Step(
            "scim",
            "Open Provisioning, set the mode to Automatic, paste the Tenant URL {scim_url}?aadOptscim062020 "
            "and the Secret Token (made when you choose Connect), then Test Connection and Start provisioning.",
        ),
        Step(
            "pull",
            "Instead of SCIM, ExaCarib can read your users and groups through Microsoft Graph. A Global "
            "Administrator opens {admin_consent_url} once and grants the read-only permissions.",
        ),
        *_COMMON_TAIL,
    ),
    metadata_hosts=("login.microsoftonline.com",),
    presets=_PRESETS,
    modes=("scim", "pull", "none"),
)

GOOGLE = Template(
    key="google",
    name="Google Workspace",
    console="Google Admin console",
    summary="Google Workspace and Cloud Identity. SAML sign-in; the Admin SDK Directory API for people and groups.",
    inputs=(
        Input(
            "idp_id",
            "SAML IdP ID (idpid)",
            r"^[A-Za-z0-9]{5,20}$",
            "C01abc2de",
            "The idpid value in the SSO URL Google shows when you add the custom SAML app.",
        ),
        Input(
            "admin_email",
            "A Google admin to read the directory as",
            r"^[^@\s]+@[^@\s]+\.[a-z]{2,63}$",
            "it-admin@example.com",
            "Needed for the Directory API pull only. Read-only.",
            ("pull",),
        ),
    ),
    saml=Saml(
        metadata_url="",
        name_id_format=SAML_EMAIL,
        name_id_value="Basic Information > Primary email",
        signing=RSA_SHA256 + " (Google's default; keep 'Signed response' ticked)",
        claims=Claims(
            "email",
            "displayName",
            "firstName",
            "lastName",
            "groups",
            "Map Primary email to email, First name to firstName, Last name to lastName, and add "
            "Group membership for the ExaCarib groups as groups. Download the IdP metadata file "
            "and upload it here.",
        ),
    ),
    oidc=None,
    scim=Scim(
        supported=False,
        note="Google provisions over SCIM only to apps in its own catalogue. Use the Directory API pull instead.",
    ),
    pull="google",
    labels={
        "acs_url": "ACS URL",
        "entity_id": "Entity ID",
        "sign_on_url": "Start URL",
        "google_client_id": "Client ID (domain-wide delegation)",
        "google_scopes": "OAuth scopes (comma-delimited)",
    },
    steps=(
        Step(
            "saml",
            "In the Google Admin console, open Apps > Web and mobile apps > Add app > Add custom SAML app "
            "and name it 'ExaCarib Connect'.",
        ),
        Step("saml", "Download the IdP metadata and upload it here. Note the idpid from the SSO URL."),
        Step(
            "saml",
            "On Service provider details, paste the ACS URL {acs_url}, the Entity ID {entity_id} and the "
            "Start URL {sign_on_url}. Tick Signed response. Name ID format EMAIL, Name ID Primary email.",
        ),
        Step(
            "saml",
            "On Attribute mapping, map Primary email to email, First name to firstName, Last name to "
            "lastName, and add the ExaCarib groups under Group membership as groups.",
        ),
        Step("saml", "Switch the app on for the organisational units or groups who should use ExaCarib."),
        Step(
            "pull",
            "For people and groups, open Security > Access and data control > API controls > Manage "
            "domain-wide delegation > Add new. Paste the Client ID {google_client_id} and the scopes "
            "{google_scopes}. They are read-only.",
        ),
        *_COMMON_TAIL,
    ),
    metadata_hosts=("accounts.google.com",),
    presets=_PRESETS,
    modes=("pull", "none"),
)

OKTA = Template(
    key="okta",
    name="Okta",
    console="Okta Admin Console",
    summary="Okta Workforce Identity. SAML or OpenID Connect sign-in; SCIM with Group Push.",
    inputs=(
        Input(
            "okta_domain",
            "Okta domain",
            r"^[a-z0-9-]+\.(okta|oktapreview|okta-emea)\.com$",
            "example.okta.com",
            "Your Okta organisation address, without https://.",
        ),
        Input(
            "app_id",
            "Okta app ID",
            r"^0oa[A-Za-z0-9]{10,24}$",
            "0oa1b2c3d4e5f6g7h8i9",
            "In the app's address in the Admin Console after you create it.",
            ("saml",),
        ),
    ),
    saml=Saml(
        metadata_url="https://{okta_domain}/app/{app_id}/sso/saml/metadata",
        name_id_format=SAML_EMAIL,
        name_id_value="Application username: Email",
        signing=RSA_SHA256 + " with SHA-256 digest; response and assertion signed",
        claims=Claims(
            "email",
            "displayName",
            "firstName",
            "lastName",
            "groups",
            "Attribute statements: email = user.email, firstName = user.firstName, lastName = "
            "user.lastName. Group attribute statement: groups, filter 'Starts with' ExaCarib.",
        ),
    ),
    oidc=Oidc(
        discovery_url="https://{okta_domain}/.well-known/openid-configuration",
        claims=Claims(
            "email",
            "name",
            "given_name",
            "family_name",
            "groups",
            "Add a groups claim to the ID token on the org authorisation server, filter 'Starts with' ExaCarib.",
        ),
    ),
    scim=Scim(
        supported=True,
        mappings=(
            ("userName", "user.email (Unique identifier field: userName)"),
            ("name.givenName", "user.firstName"),
            ("name.familyName", "user.lastName"),
            ("emails[primary].value", "user.email"),
            ("displayName", "user.displayName"),
            ("externalId", "Okta user ID"),
            ("Group displayName", "Push Groups name"),
        ),
        quirks=(
            'Looks a person up with GET /Users?filter=userName eq "..." before it creates them. Handled.',
            "Deactivates with PATCH active=false and never deletes. The person is signed out at once.",
            "Group Push renames with PATCH replace and a value {id, displayName} without a path. Handled.",
            "Group Push adds and removes members with PATCH members operations. Handled.",
            "Sends a random password in the create request. Ignored: people sign in through Okta.",
        ),
    ),
    pull=None,
    labels={
        "acs_url": "Single sign-on URL",
        "entity_id": "Audience URI (SP Entity ID)",
        "redirect_uri": "Sign-in redirect URIs",
        "sign_on_url": "Initiate login URI",
        "scim_url": "SCIM connector base URL",
        "scim_token": "Bearer token (Authentication Mode: HTTP Header)",
    },
    steps=(
        Step(
            "saml",
            "In the Okta Admin Console open Applications > Create App Integration > SAML 2.0 and name it "
            "'ExaCarib Connect'.",
        ),
        Step(
            "saml",
            "Paste the Single sign-on URL {acs_url} (tick 'Use this for Recipient URL and Destination "
            "URL') and the Audience URI {entity_id}. Name ID format EmailAddress, Application username "
            "Email.",
        ),
        Step(
            "saml",
            "Add the attribute statements and the groups statement shown below, then copy the app ID "
            "from the address bar.",
        ),
        Step(
            "oidc",
            "Create an OIDC Web Application. Sign-in redirect URI {redirect_uri}, Initiate login URI "
            "{sign_on_url}. Paste the client ID and secret here; the secret goes to the sign-in gateway "
            "only.",
        ),
        Step(
            "scim",
            "On the General tab tick 'Enable SCIM provisioning'. On Provisioning > Integration paste the "
            "SCIM connector base URL {scim_url}, Unique identifier field userName, actions Push New Users, "
            "Push Profile Updates and Push Groups, Authentication Mode HTTP Header with the Bearer token "
            "made when you choose Connect.",
        ),
        Step(
            "scim",
            "Under Provisioning > To App, switch on Create Users, Update User Attributes and Deactivate "
            "Users. Assign the ExaCarib groups and add them under Push Groups.",
        ),
        *_COMMON_TAIL,
    ),
    metadata_hosts=(".okta.com", ".oktapreview.com", ".okta-emea.com"),
    presets=_PRESETS,
    modes=("scim", "none"),
)

JUMPCLOUD = Template(
    key="jumpcloud",
    name="JumpCloud",
    console="JumpCloud Admin Portal",
    summary="JumpCloud directory. SAML or OpenID Connect sign-in; SCIM (Identity Management) for people.",
    inputs=(),
    saml=Saml(
        metadata_url="",
        name_id_format=SAML_EMAIL,
        name_id_value="email",
        signing=RSA_SHA256 + "; choose 'Sign Assertion'",
        claims=Claims(
            "email",
            "displayName",
            "firstname",
            "lastname",
            "memberOf",
            "Add attributes email, firstname and lastname, and tick 'Include group attribute' with the "
            "name memberOf. Export the metadata and upload it here.",
        ),
    ),
    oidc=Oidc(
        discovery_url="https://oauth.id.jumpcloud.com/.well-known/openid-configuration",
        claims=Claims("email", "name", "given_name", "family_name", "groups"),
    ),
    scim=Scim(
        supported=True,
        mappings=(
            ("userName", "email"),
            ("name.givenName", "firstname"),
            ("name.familyName", "lastname"),
            ("emails[primary].value", "email"),
            ("externalId", "JumpCloud user ID"),
            ("Group displayName", "user group name"),
        ),
        quirks=(
            'Activation sends a test GET /Users?filter=userName eq "<test email>". An empty list is the right '
            "answer. Handled.",
            "Deactivates with PATCH active=false (a boolean). Handled; the person is signed out at once.",
            "Group membership arrives as PATCH members add and remove. Handled.",
        ),
    ),
    pull=None,
    labels={
        "entity_id": "SP Entity ID",
        "acs_url": "ACS URLs",
        "sign_on_url": "Login URL",
        "redirect_uri": "Redirect URIs",
        "scim_url": "Base URL",
        "scim_token": "Token Key",
    },
    steps=(
        Step(
            "saml",
            "In the JumpCloud Admin Portal open SSO Applications > Add New Application > Custom "
            "Application, choose Manage Single Sign-On (SSO) with SAML 2.0, name it 'ExaCarib Connect'.",
        ),
        Step(
            "saml",
            "Set the SP Entity ID {entity_id}, the ACS URL {acs_url} and the Login URL {sign_on_url}. "
            "Choose a unique IdP Entity ID, SAMLSubject NameID email, signature 'Sign Assertion'.",
        ),
        Step(
            "saml",
            "Add the attributes shown below and tick 'Include group attribute' (memberOf). Save, then "
            "Export Metadata and upload the file here.",
        ),
        Step(
            "oidc",
            "Choose 'Manage Single Sign-On (SSO) with OIDC' instead. Redirect URI {redirect_uri}, Login "
            "URL {sign_on_url}. Paste the client ID and secret here.",
        ),
        Step(
            "scim",
            "On Identity Management choose SCIM 2.0, paste the Base URL {scim_url} and the Token Key made "
            "when you choose Connect, give a test email and Activate.",
        ),
        Step("all", "Under User Groups, bind the ExaCarib groups to the application."),
        *_COMMON_TAIL,
    ),
    metadata_hosts=("oauth.id.jumpcloud.com", "sso.jumpcloud.com"),
    presets=_PRESETS,
    modes=("scim", "none"),
)

ONELOGIN = Template(
    key="onelogin",
    name="OneLogin",
    console="OneLogin Administration",
    summary="OneLogin. SAML or OpenID Connect sign-in; SCIM through the SCIM Provisioner connector.",
    inputs=(
        Input(
            "subdomain",
            "OneLogin subdomain",
            r"^[a-z0-9-]{2,63}$",
            "example",
            "The part before .onelogin.com in your OneLogin address.",
        ),
        Input(
            "app_id",
            "OneLogin app ID",
            r"^[0-9]{3,12}$",
            "1234567",
            "The number in the app's address after you save it.",
            ("saml",),
        ),
    ),
    saml=Saml(
        metadata_url="https://{subdomain}.onelogin.com/saml/metadata/{app_id}",
        name_id_format=SAML_EMAIL,
        name_id_value="Email",
        signing="SHA-256 ('SAML Signature Algorithm: SHA-256'; sign the assertion)",
        claims=Claims(
            "Email",
            "DisplayName",
            "FirstName",
            "LastName",
            "memberOf",
            "Add parameters Email, FirstName and LastName, and memberOf (MemberOf, 'Include in SAML "
            "assertion', semicolon-delimited input).",
        ),
    ),
    oidc=Oidc(
        discovery_url="https://{subdomain}.onelogin.com/oidc/2/.well-known/openid-configuration",
        claims=Claims("email", "name", "given_name", "family_name", "groups"),
    ),
    scim=Scim(
        supported=True,
        mappings=(
            ("userName", "email"),
            ("name.givenName", "firstname"),
            ("name.familyName", "lastname"),
            ("emails[primary].value", "email"),
            ("externalId", "OneLogin user ID"),
            ("Group displayName", "Groups parameter (set by Rules)"),
        ),
        quirks=(
            "Updates people with PUT (a full replace). Anything left out of the SCIM JSON Template is cleared, so "
            "the template must send name.givenName and name.familyName.",
            "Groups are pushed by Rules ('Set Groups in ExaCarib Connect'), not by group assignment.",
            'Some versions send single values wrapped in a list ([{"value": ...}]). Handled.',
        ),
    ),
    pull=None,
    labels={
        "entity_id": "Audience (EntityID)",
        "acs_url": "ACS (Consumer) URL",
        "acs_validator": "ACS (Consumer) URL Validator",
        "recipient": "Recipient",
        "sign_on_url": "Login URL",
        "redirect_uri": "Redirect URI's",
        "scim_url": "SCIM Base URL",
        "scim_token": "SCIM Bearer Token",
    },
    steps=(
        Step(
            "saml",
            "In OneLogin Administration open Applications > Add App and choose 'SCIM Provisioner with "
            "SAML (SCIM v2 Enterprise)'. Name it 'ExaCarib Connect'.",
        ),
        Step(
            "saml",
            "On Configuration paste the Audience {entity_id}, the Recipient {recipient}, the ACS (Consumer) "
            "URL Validator {acs_validator} and the ACS (Consumer) URL {acs_url}. Set SAML Signature "
            "Algorithm to SHA-256.",
        ),
        Step(
            "oidc",
            "Or add 'OpenId Connect (OIDC)', set the Login URL {sign_on_url} and Redirect URI "
            "{redirect_uri}, and paste the client ID and secret here.",
        ),
        Step(
            "scim",
            "Still on Configuration, paste the SCIM Base URL {scim_url} and the SCIM Bearer Token made "
            "when you choose Connect. In the SCIM JSON Template keep name.givenName and name.familyName. "
            "Then Enable the API connection.",
        ),
        Step(
            "scim",
            "On Provisioning tick 'Enable provisioning' and set 'When users are deleted' to Suspend. Add a "
            "Rule that sets Groups in ExaCarib Connect from your OneLogin roles or groups.",
        ),
        *_COMMON_TAIL,
    ),
    metadata_hosts=(".onelogin.com",),
    presets=_PRESETS,
    modes=("scim", "none"),
)

PING = Template(
    key="ping",
    name="Ping Identity",
    console="PingOne admin console",
    summary="PingOne (and PingFederate). SAML or OpenID Connect sign-in; SCIM outbound provisioning.",
    inputs=(
        Input(
            "region",
            "PingOne region domain",
            r"^(com|eu|ca|asia|com\.au|sg)$",
            "eu",
            "The ending of your PingOne address: com, eu, ca, asia, com.au or sg.",
        ),
        Input(
            "env_id",
            "Environment ID",
            _GUID,
            "22222222-2222-2222-2222-222222222222",
            "PingOne > Environment > Properties.",
        ),
        Input(
            "app_id",
            "Application ID",
            _GUID,
            "33333333-3333-3333-3333-333333333333",
            "On the application's Overview after you save it.",
            ("saml",),
        ),
    ),
    saml=Saml(
        metadata_url="https://auth.pingone.{region}/{env_id}/saml20/metadata/{app_id}",
        name_id_format=SAML_EMAIL,
        name_id_value="saml_subject = Email Address",
        signing="RSA_SHA256; sign the assertion",
        claims=Claims(
            "email",
            "displayName",
            "given_name",
            "family_name",
            "memberOf",
            "Attribute mappings: email, given_name, family_name, and memberOf from Group Names.",
        ),
    ),
    oidc=Oidc(
        discovery_url="https://auth.pingone.{region}/{env_id}/as/.well-known/openid-configuration",
        claims=Claims(
            "email",
            "name",
            "given_name",
            "family_name",
            "groups",
            "Add a groups attribute mapped from Group Names to the ID token.",
        ),
    ),
    scim=Scim(
        supported=True,
        mappings=(
            ("userName", "Email Address"),
            ("name.givenName", "Given Name"),
            ("name.familyName", "Family Name"),
            ("emails[primary].value", "Email Address"),
            ("active", "Enabled"),
            ("externalId", "User ID"),
        ),
        quirks=(
            'The default Users Filter Expression is userName Eq "%s" with a capital Eq. Handled: SCIM operators '
            "are case-insensitive.",
            "Some paths arrive with the core schema prefix (urn:ietf:params:scim:schemas:core:2.0:User:active). "
            "Handled.",
            "Group membership arrives as PATCH members add and remove. Handled.",
        ),
    ),
    pull=None,
    labels={
        "acs_url": "ACS URLs",
        "entity_id": "Entity ID",
        "redirect_uri": "Redirect URIs",
        "sign_on_url": "Initiate Login URI",
        "scim_url": "SCIM BASE URL",
        "scim_token": "OAuth Access Token (Authentication Method: OAuth 2 Bearer Token)",
        "scim_filter": "Users Filter Expression",
    },
    steps=(
        Step(
            "saml",
            "In PingOne open Applications > Applications > Add, choose SAML Application and name it "
            "'ExaCarib Connect'. Choose 'Manually enter' for the configuration.",
        ),
        Step(
            "saml",
            "Paste the ACS URL {acs_url} and the Entity ID {entity_id}. Signing RSA_SHA256, sign the "
            "assertion. Save and copy the Application ID.",
        ),
        Step("saml", "On Attribute Mappings set saml_subject to Email Address and add the attributes shown below."),
        Step(
            "oidc",
            "Or add an OIDC Web App. Redirect URI {redirect_uri}, Initiate Login URI {sign_on_url}. Paste "
            "the client ID and secret here.",
        ),
        Step(
            "scim",
            "Open Integrations > Provisioning > New Connection > SCIM Outbound. SCIM BASE URL {scim_url}, "
            "Authentication Method OAuth 2 Bearer Token with the token made when you choose Connect, "
            "Users Filter Expression {scim_filter}. Add a rule for the ExaCarib groups.",
        ),
        *_COMMON_TAIL,
    ),
    metadata_hosts=(
        "auth.pingone.com",
        "auth.pingone.eu",
        "auth.pingone.ca",
        "auth.pingone.asia",
        "auth.pingone.com.au",
        "auth.pingone.sg",
    ),
    presets=_PRESETS,
    modes=("scim", "none"),
)

AUTH0 = Template(
    key="auth0",
    name="Auth0",
    console="Auth0 Dashboard",
    summary="Auth0 tenants. OpenID Connect (or the SAML2 Web App add-on) for sign-in. Auth0 does not push SCIM.",
    inputs=(
        Input(
            "auth0_domain",
            "Auth0 domain",
            r"^[a-z0-9-]+(\.[a-z]{2})?\.auth0\.com$",
            "example.eu.auth0.com",
            "Your tenant domain from Settings, without https://.",
        ),
        Input(
            "client_id",
            "Application client ID",
            r"^[A-Za-z0-9_-]{16,64}$",
            "AbCdEf0123456789AbCdEf0123456789",
            "The application's Client ID.",
            ("saml",),
        ),
    ),
    saml=Saml(
        metadata_url="https://{auth0_domain}/samlp/metadata/{client_id}",
        name_id_format=SAML_EMAIL,
        name_id_value="email (nameIdentifierProbes: email)",
        signing='rsa-sha256: set "signatureAlgorithm": "rsa-sha256" and "digestAlgorithm": "sha256" in the add-on '
        "settings (the add-on's default is SHA-1)",
        claims=Claims(
            f"{_MS}/emailaddress",
            f"{_MS}/name",
            f"{_MS}/givenname",
            f"{_MS}/surname",
            "http://schemas.xmlsoap.org/claims/Group",
            "The add-on's default mappings. Groups come from an Action that sets app_metadata.groups.",
        ),
    ),
    oidc=Oidc(
        discovery_url="https://{auth0_domain}/.well-known/openid-configuration",
        claims=Claims(
            "email",
            "name",
            "given_name",
            "family_name",
            "https://exacarib.com/groups",
            "Auth0 has no groups claim of its own. Add a post-login Action that sets the custom claim "
            "https://exacarib.com/groups on the ID token.",
        ),
    ),
    scim=Scim(
        supported=False,
        note="Auth0 does not send SCIM. Accounts are made the first time someone from an approved domain signs in, "
        "with member rights only; admin rights still need an approved mapping.",
    ),
    pull=None,
    labels={
        "redirect_uri": "Allowed Callback URLs",
        "sign_on_url": "Application Login URI",
        "acs_url": "Application Callback URL (SAML2 Web App add-on)",
        "entity_id": "audience (SAML2 Web App add-on settings)",
    },
    steps=(
        Step(
            "oidc",
            "In the Auth0 Dashboard open Applications > Create Application > Regular Web Application and "
            "name it 'ExaCarib Connect'.",
        ),
        Step(
            "oidc",
            "In Settings paste the Allowed Callback URLs {redirect_uri} and the Application Login URI "
            "{sign_on_url}. Paste the Client ID and Client Secret here; the secret goes to the sign-in "
            "gateway only.",
        ),
        Step("oidc", "Add a post-login Action that puts the person's groups in the claim https://exacarib.com/groups."),
        Step(
            "saml",
            "Or, on the application's Addons tab, switch on SAML2 Web App. Application Callback URL "
            "{acs_url}; in Settings set audience {entity_id}, signatureAlgorithm rsa-sha256 and "
            "digestAlgorithm sha256.",
        ),
        *_COMMON_TAIL,
    ),
    metadata_hosts=(".auth0.com",),
    presets=_PRESETS,
    default_protocol="oidc",
    modes=("none",),
)

ACTIVE_DIRECTORY = Template(
    key="active-directory",
    name="Active Directory (on-premises)",
    console="AD FS Management and Active Directory Users and Computers",
    summary="Windows Server Active Directory. AD FS for SAML sign-in; scheduled LDAPS sync for people and groups.",
    inputs=(
        Input(
            "adfs_host",
            "AD FS host name",
            _HOST,
            "adfs.example.com",
            "The public name of your AD FS farm, without https://.",
            ("saml",),
        ),
    ),
    saml=Saml(
        metadata_url="https://{adfs_host}/FederationMetadata/2007-06/FederationMetadata.xml",
        name_id_format=SAML_EMAIL,
        name_id_value="Transform E-Mail-Addresses to Name ID, format Email",
        signing="SHA-256 (relying party trust > Advanced > Secure hash algorithm)",
        claims=Claims(
            f"{_MS}/emailaddress",
            f"{_MS}/name",
            f"{_MS}/givenname",
            f"{_MS}/surname",
            "http://schemas.xmlsoap.org/claims/Group",
            "Claim rule 'Send LDAP Attributes as Claims': E-Mail-Addresses, Given-Name, Surname, Display-Name and "
            "Token-Groups - Unqualified Names (Group).",
        ),
    ),
    oidc=None,
    scim=Scim(supported=False, note="Active Directory does not send SCIM. ExaCarib reads it over LDAPS instead."),
    pull="ldap",
    labels={
        "entity_id": "Relying party trust identifier",
        "acs_url": "Relying party SAML 2.0 SSO service URL",
        "sp_metadata_url": "Federation metadata address (relying party)",
        "ldap_attributes": "Attributes read (read-only)",
    },
    steps=(
        Step(
            "saml",
            "In AD FS Management choose Add Relying Party Trust > Claims aware > 'Import data about the "
            "relying party published online' and give {sp_metadata_url}. If AD FS can't reach it, enter "
            "the identifier {entity_id} and the SAML 2.0 SSO service URL {acs_url} by hand.",
        ),
        Step(
            "saml",
            "Add the claim rules shown below, and a Transform rule from E-Mail Address to Name ID with the "
            "Email format. Set the secure hash algorithm to SHA-256.",
        ),
        Step(
            "ldap",
            "Create a read-only service account (for example svc-exacarib) in a locked-down OU. It needs "
            "only 'Read' on the users and groups it should see; it never needs to write.",
        ),
        Step(
            "ldap",
            "Make sure a domain controller answers LDAPS on port 636 with a certificate your public or "
            "internal CA signed, and that ExaCarib's addresses can reach it (VPN or firewall rule).",
        ),
        Step(
            "ldap",
            "Enter the host, base DN, bind DN and password below. The password goes into ExaCarib's "
            "encrypted store and is never shown again. If your CA is internal, paste its certificate.",
        ),
        Step(
            "ldap",
            "Put the people who should use ExaCarib in groups whose names start with the group prefix "
            "(for example 'ExaCarib Agents'). The sync runs on a schedule; someone removed or disabled is "
            "signed out at once.",
        ),
        *_COMMON_TAIL,
    ),
    presets=_PRESETS,
    modes=("ldap", "none"),
)

LDAP = Template(
    key="ldap",
    name="Generic LDAP",
    console="your LDAP server (OpenLDAP, 389 Directory Server, FreeIPA and others)",
    summary="Any LDAPv3 directory over LDAPS. Scheduled sync for people and groups; pair it with any SAML or "
    "OpenID Connect sign-in, or passwords.",
    inputs=(),
    saml=None,
    oidc=None,
    scim=Scim(supported=False, note="LDAP servers do not send SCIM. ExaCarib reads the directory over LDAPS."),
    pull="ldap",
    labels={"ldap_attributes": "Attributes read (read-only)"},
    steps=(
        Step("ldap", "Create a read-only bind account that can read people and groups under your base DN."),
        Step(
            "ldap",
            "Make sure the server answers LDAPS on port 636 and that ExaCarib's addresses can reach it. "
            "Plain LDAP is refused outside ExaCarib's lab.",
        ),
        Step(
            "ldap",
            "Enter the host, base DN, bind DN and password below. People are read with the user filter "
            "(default: inetOrgPerson with an email address); groups are groupOfNames or "
            "groupOfUniqueNames whose names start with the group prefix.",
        ),
        Step(
            "ldap",
            "Disabled accounts (pwdAccountLockedTime or nsAccountLock) and people who disappear from the "
            "directory are switched off and signed out at once.",
        ),
        *_COMMON_TAIL,
    ),
    presets=_PRESETS,
    default_protocol="",
    modes=("ldap",),
)

TEMPLATES: dict[str, Template] = {
    t.key: t for t in (ENTRA, GOOGLE, OKTA, JUMPCLOUD, ONELOGIN, PING, AUTH0, ACTIVE_DIRECTORY, LDAP)
}

# Read-only scopes and permissions the pull connectors ask for.
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
    "https://www.googleapis.com/auth/admin.directory.group.readonly",
    "https://www.googleapis.com/auth/admin.directory.group.member.readonly",
)
GRAPH_PERMISSIONS = ("User.Read.All", "GroupMember.Read.All")

LDAP_ATTRIBUTES = {
    "ad": "mail, userPrincipalName, givenName, sn, displayName, objectGUID, userAccountControl; groups: cn, member",
    "openldap": "mail, givenName, sn, cn, displayName, entryUUID, pwdAccountLockedTime, nsAccountLock; "
    "groups: cn, member, uniqueMember",
}


class TemplateError(ValueError):
    """Bad input for a template. The message is safe to show."""


def get(key: str) -> Template:
    t = TEMPLATES.get(key)
    if t is None:
        raise TemplateError("There is no directory template with that name.")
    return t


_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)\}")


def fill(pattern: str, values: dict[str, str]) -> str:
    """Put values into {placeholders}; unknown ones stay as they are."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1)) or m.group(0), pattern)


def missing(pattern: str, values: dict[str, str]) -> list[str]:
    return [k for k in _PLACEHOLDER.findall(pattern) if not values.get(k)]


def check_inputs(t: Template, given: dict[str, str]) -> dict[str, str]:
    """Validate the business's inputs against the template's patterns (empty values are allowed)."""
    out: dict[str, str] = {}
    known = {i.key: i for i in t.inputs}
    for k, v in (given or {}).items():
        if k not in known:
            raise TemplateError(f"{t.name} has no setting called {k}.")
        v = str(v or "").strip()
        if not v:
            continue
        if k.endswith("_domain") or k in ("adfs_host", "subdomain"):
            v = v.lower().removeprefix("https://").rstrip("/")
        if not re.fullmatch(known[k].pattern, v):
            raise TemplateError(f"{known[k].label} doesn't look right. Example: {known[k].example}.")
        out[k] = v
    return out


@dataclass
class Context:
    """What a business's values are made from."""

    public_url: str  # the portal address (EXA_PUBLIC_URL)
    issuer: str  # the sign-in gateway realm (EXA_OIDC_ISSUER)
    alias: str  # this business's identity provider alias at the gateway
    inputs: dict[str, str] = field(default_factory=dict)
    graph_client_id: str = ""
    google_client_id: str = ""


def our_values(t: Template, ctx: Context) -> dict[str, str]:
    """Everything the business pastes into its provider's console, made for this business."""
    gw = ctx.issuer
    pub = ctx.public_url
    broker = f"{gw}/broker/{ctx.alias}/endpoint" if gw else ""
    v = {
        "entity_id": gw,
        "acs_url": broker,
        "recipient": broker,
        "acs_validator": "^" + re.escape(broker) + "$" if broker else "",
        "redirect_uri": broker,
        "sp_metadata_url": f"{broker}/descriptor" if broker else "",
        "sign_on_url": f"{pub}/api/v1/auth/oidc/start?idp={ctx.alias}&next=/" if pub else "",
        "scim_url": f"{pub}/api/v1/scim/v2" if pub else "",
        "scim_token": "Made when you choose Connect. Shown once.",
        "scim_filter": 'userName eq "%s"',
        "name_id_format": t.saml.name_id_format if t.saml else "",
    }
    if t.pull == "graph":
        tenant = ctx.inputs.get("tenant_id") or "{tenant_id}"
        v["admin_consent_url"] = (
            f"https://login.microsoftonline.com/{tenant}/adminconsent?client_id={ctx.graph_client_id}"
            if ctx.graph_client_id
            else ""
        )
        v["graph_permissions"] = ", ".join(GRAPH_PERMISSIONS) + " (application, read-only)"
    if t.pull == "google":
        v["google_client_id"] = ctx.google_client_id
        v["google_scopes"] = ",".join(GOOGLE_SCOPES)
    if t.pull == "ldap":
        v["ldap_attributes"] = LDAP_ATTRIBUTES["ad" if t.key == "active-directory" else "openldap"]
    return v


def render(t: Template, ctx: Context, protocol: str, mode: str) -> dict:
    """The guided set-up for one business: values with the provider's labels, the
    filled-in metadata/discovery URL, claims, SCIM details, steps and presets."""
    ours = our_values(t, ctx)
    # Only show what this protocol and mode use.
    wanted = set(t.labels)
    if protocol != "saml":
        wanted -= {"entity_id", "acs_url", "recipient", "acs_validator", "sp_metadata_url"}
    if protocol != "oidc":
        wanted -= {"redirect_uri"}
    if mode != "scim":
        wanted -= {"scim_url", "scim_token", "scim_filter"}
    if mode != "pull":
        wanted -= {"admin_consent_url", "google_client_id", "google_scopes"}
    unavailable = {
        "entity_id": "Set when ExaCarib's sign-in gateway is configured.",
        "acs_url": "Set when ExaCarib's sign-in gateway is configured.",
        "recipient": "Set when ExaCarib's sign-in gateway is configured.",
        "acs_validator": "Set when ExaCarib's sign-in gateway is configured.",
        "redirect_uri": "Set when ExaCarib's sign-in gateway is configured.",
        "sp_metadata_url": "Set when ExaCarib's sign-in gateway is configured.",
        "sign_on_url": "Set when ExaCarib's public address is configured.",
        "scim_url": "Set when ExaCarib's public address is configured.",
        "admin_consent_url": "Waiting for ExaCarib to register its Microsoft app.",
        "google_client_id": "Waiting for ExaCarib to set up its Google service account.",
    }
    values = [
        {
            "key": k,
            "label": t.labels[k],
            "value": ours.get(k, ""),
            "missing": "" if ours.get(k) else unavailable.get(k, ""),
        }
        for k in t.labels
        if k in wanted
    ]
    fills = {**ours, **ctx.inputs}
    spec = t.saml if protocol == "saml" else t.oidc if protocol == "oidc" else None
    provider_url = ""
    needs: list[str] = []
    if spec is not None:
        pattern = spec.metadata_url if isinstance(spec, Saml) else spec.discovery_url
        provider_url = fill(pattern, fills) if pattern else ""
        needs = missing(pattern, fills) if pattern else []
    active = {"all", protocol, mode}
    steps = [fill(s.text, fills) for s in t.steps if s.when in active]
    return {
        "provider": t.key,
        "name": t.name,
        "console": t.console,
        "summary": t.summary,
        "protocols": t.protocols,
        "modes": list(t.modes),
        "pull": t.pull,
        "protocol": protocol,
        "mode": mode,
        "inputs": [
            {
                "key": i.key,
                "label": i.label,
                "example": i.example,
                "help": i.help,
                "needed_for": list(i.needed_for),
                "value": ctx.inputs.get(i.key, ""),
            }
            for i in t.inputs
        ],
        "values": values,
        "provider_metadata": {
            "kind": "saml" if protocol == "saml" else "oidc" if protocol == "oidc" else "",
            "url": provider_url,
            "needs": needs,
            "upload": bool(isinstance(spec, Saml) and not spec.metadata_url),
        },
        "saml": _spec_out(t.saml),
        "oidc": _spec_out(t.oidc),
        "scim": {
            "supported": t.scim.supported,
            "mappings": [{"ours": a, "theirs": b} for a, b in t.scim.mappings],
            "quirks": list(t.scim.quirks),
            "note": t.scim.note,
        },
        "steps": steps,
        "presets": [
            {"id": p.id, "group": p.group, "seat": p.seat, "business_admin": p.business_admin, "note": p.note}
            for p in t.presets
        ],
    }


def _spec_out(spec: Saml | Oidc | None) -> dict | None:
    if spec is None:
        return None
    claims = {
        "email": spec.claims.email,
        "name": spec.claims.name,
        "given_name": spec.claims.given_name,
        "family_name": spec.claims.family_name,
        "groups": spec.claims.groups,
        "note": spec.claims.note,
    }
    if isinstance(spec, Saml):
        return {
            "metadata_url": spec.metadata_url,
            "name_id_format": spec.name_id_format,
            "name_id_value": spec.name_id_value,
            "signing": spec.signing,
            "claims": claims,
        }
    return {"discovery_url": spec.discovery_url, "scopes": spec.scopes, "signing": spec.signing, "claims": claims}
