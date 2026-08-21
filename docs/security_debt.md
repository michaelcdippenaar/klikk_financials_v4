# Security debt register

## HIGH — Xero OAuth callback state and log handling

The existing Xero OAuth callback does not validate a signed, expiring, one-time `state` value that
binds the browser actor and selected entity. It also logs the authorization code during callback
processing. This creates login-CSRF/cross-session ambiguity and unnecessary credential-material log
exposure.

The Web API v2 candidate does not expose connection initiation, credential provisioning, or the
callback. Before a v2 connection workflow is added, Backend must implement a durable authorization
session, signed one-time state validation, safe expiry/replay handling, entity/admin authorization,
and removal/redaction of authorization-code logging. This debt is not resolved by the ingest
command candidate and must remain release-visible.
