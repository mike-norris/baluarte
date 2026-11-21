"""The auditable issue register (roadmap M12, ADR-0015, docs/ISSUE_REGISTER.md).

An issue is auditable when a selection rule of its workspace matches it, or someone included it
with a reason; an exclusion, also with a reason, takes a rule-selected issue out but keeps it
listed as excluded. Each listed issue says why, and which category it falls in: an engineering
issue, a security finding, an exception or a remediation task.
"""
