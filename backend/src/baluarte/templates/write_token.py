"""The template write token and the deployment switch that allows applying change sets
(roadmap M14.4, docs/CONFIGURATION_TEMPLATES.md#execution).

- ``BALUARTE_TEMPLATE_WRITES`` (``true`` or ``false``, default ``false``) is set only in the
  deployment's environment. While it is false no change set is applied, continued or rolled
  back, whatever is stored.
- The token is separate from the read token and from the label and remediation write tokens:
  a change set can widen visibility or remove a protection, which those never can. It is stored
  and checked as described in ``baluarte.configuration.write_tokens``, and only the change set
  applier (``baluarte.templates.execute``) decrypts it.

Storing, replacing and removing it are audited as ``settings.template_write_token_*`` without
the token itself.
"""

from baluarte.configuration.write_tokens import WriteToken

TEMPLATE_WRITES = "BALUARTE_TEMPLATE_WRITES"
TOKEN = WriteToken(name="template_write_token", switch=TEMPLATE_WRITES, writes="template writes")
