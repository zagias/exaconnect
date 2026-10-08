"""Jibsy self-service (ADR 0037): "My settings" for a business's staff and the
help centre for the business's own customers.

- staff: a person's own profile, language, notifications, availability and
  sessions. The person is always the signed-in one.
- branding: the one place the help centre reads a business's name and colours.
- helpcentre: the public help centre (published articles, search, Ask, contact
  form, hours and channel links) and its admin settings.
- enduser: end-user sign-in (emailed one-time link or the business's signed
  token) and what a signed-in end user may see and do.
"""

from . import branding, enduser, helpcentre, staff  # noqa: F401
