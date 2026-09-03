"""Who is behind a credential — the one answer, for everything that stamps a name.

A comment, a saved view, an audit note: all of them record WHO. There are three
kinds of caller in this project and they need three different answers:

  * a person signed in as themselves (the console JWT, an auditor's session)
    — their username IS the answer.
  * an agent on the shared MCP service token — one credential, many
    workstreams, so the caller must SAY which one it is
    (``claude:year-end-audit``, ``codex:fy2026-account-allocation``). MC reads
    those strings to tell the workstreams apart, so they are load-bearing.
  * a shared TOOL credential held by one known person — the Excel add-in's
    ``excel-addin`` token. The username names the tool, and the person behind
    it is a property of the ACCOUNT, declared server-side in
    ``settings.SERVICE_ACCOUNT_OPERATORS``.

This module owns the third case, because it is the one that must not be
answered by the client. The pane used to ask the operator to type their name
into a text box, which produced `ewffew` (×12), `test`, `test2`, MC's notes
split across two spellings of himself, and 55 rows authored by nobody. A field
the client controls is a field that disagrees with the credential; the fix is
for the client not to have one.

Deliberately NOT a mapping the request can influence: no header, no body field,
no query parameter feeds it. A copied token cannot claim to be someone else —
it can only ever be the operator its own account is mapped to.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)

_warned = set()


def _warn_once(key, *args):
    """Log a misconfiguration once per process, not once per comment."""
    if key in _warned:
        return
    _warned.add(key)
    logger.error(*args)


def service_operator(user):
    """The person behind a shared service credential, or None.

    None for every caller that is not a mapped service account — a real person,
    an anonymous request, the MCP ``ServiceAccount`` — so callers keep whatever
    behaviour they had before this module existed.

    The configured operator must name a real, ACTIVE user. A mapping that names
    a deleted or disabled account returns None (with a logged error) rather
    than stamping a name nobody can log in as: a typo in configuration must not
    be able to invent an identity in the register.
    """
    username = getattr(user, 'username', '') or ''
    if not username:
        return None
    mapping = getattr(settings, 'SERVICE_ACCOUNT_OPERATORS', None) or {}
    operator = (mapping.get(username) or '').strip()
    if not operator:
        return None
    # Only now does this touch the database: an unmapped caller — which is
    # nearly all of them — costs one dict lookup.
    model = get_user_model()
    if not model.objects.filter(username=operator, is_active=True).exists():
        _warn_once(
            'operator:%s' % operator,
            'SERVICE_ACCOUNT_OPERATORS maps service account %r to %r, which is not an '
            'active user. Comments from that credential will be stamped with the '
            'service account name until the mapping is corrected.',
            username, operator,
        )
        return None
    return operator
