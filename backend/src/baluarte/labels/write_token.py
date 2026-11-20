"""The label write token and the deployment switch that allows label writes (ADR-0009).

- ``BALUARTE_LABEL_WRITES`` (``true`` or ``false``, default ``false``) is set only in the
  deployment's environment. While it is false no label plan can be applied, whatever is stored.
- The token is a sealed secret entered by an administrator, stored and checked as described in
  ``baluarte.configuration.write_tokens``. Only the label and issue plan appliers decrypt it.

Storing, replacing and removing it are audited as ``settings.label_write_token_*`` without the
token itself.
"""

from baluarte.configuration.write_tokens import TokenInvalid, TokenStatus, WriteToken, checked

__all__ = [
    "LABEL_WRITES",
    "TOKEN",
    "TokenInvalid",
    "TokenStatus",
    "check",
    "checked",
    "load",
    "remove",
    "save",
    "status",
    "writes_enabled",
]

LABEL_WRITES = "BALUARTE_LABEL_WRITES"
TOKEN = WriteToken(name="label_write_token", switch=LABEL_WRITES, writes="label writes")

writes_enabled = TOKEN.writes_enabled
status = TOKEN.status
check = TOKEN.check
save = TOKEN.save
remove = TOKEN.remove
load = TOKEN.load
