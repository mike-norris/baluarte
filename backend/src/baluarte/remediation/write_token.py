"""The remediation write token and the deployment switch that allows remediation (ADR-0012).

- ``BALUARTE_REMEDIATION_WRITES`` (``true`` or ``false``, default ``false``) is set only in the
  deployment's environment. While it is false no remediation plan can be applied, whatever is
  stored.
- The token is separate from the read token and from the label write token: restricting
  visibility needs the Owner role, which label writes never should. It is stored and checked as
  described in ``baluarte.configuration.write_tokens``, and only the remediation applier
  (``baluarte.remediation.execute``) decrypts it.

Storing, replacing and removing it are audited as ``settings.remediation_write_token_*``
without the token itself.
"""

from baluarte.configuration.write_tokens import WriteToken

REMEDIATION_WRITES = "BALUARTE_REMEDIATION_WRITES"
TOKEN = WriteToken(
    name="remediation_write_token", switch=REMEDIATION_WRITES, writes="remediation writes"
)
