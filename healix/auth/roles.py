"""User roles: named identities a run can log in as, one credential each.

A role is only a *name* in the run config (``"roles": ["admin", "standard"]``), so the config stays
safe to commit. The credentials for a role live in the environment (a git-ignored ``.env``), under
the role's name in capitals::

    WEBLIB_LOGIN_USERNAME_ADMIN=...       WEBLIB_LOGIN_PASSWORD_ADMIN=...
    WEBLIB_LOGIN_USERNAME_STANDARD=...    WEBLIB_LOGIN_PASSWORD_STANDARD=...

``anonymous`` is a reserved role with no credential: it never logs in, which is how to see what
a visitor who is not signed in can reach.

Not to be confused with an *element role* (``healix.healing.roles``: ``button:save``), which names
an element on a page. A user role names who is looking at it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

USERNAME_ENV_PREFIX = "WEBLIB_LOGIN_USERNAME"
PASSWORD_ENV_PREFIX = "WEBLIB_LOGIN_PASSWORD"

ANONYMOUS = "anonymous"

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")


class RoleError(ValueError):
    """A role name that cannot be used."""


def env_suffix(role: str) -> str:
    """``admin`` -> ``ADMIN``, ``read-only`` -> ``READ_ONLY``."""
    return _NON_ALNUM.sub("_", role).upper()


def credential_env_names(role: str) -> tuple[str, str]:
    """The environment variables that hold ``role``'s username and password."""
    suffix = env_suffix(role)
    return f"{USERNAME_ENV_PREFIX}_{suffix}", f"{PASSWORD_ENV_PREFIX}_{suffix}"


def validate_roles(names: object) -> tuple[str, ...]:
    """``names`` as a tuple of usable, distinct role names, or a ``RoleError`` saying what is wrong.

    A name is lowercase letters, digits, ``-`` and ``_`` (starting with a letter, at most 32
    characters), because it becomes a directory name and part of an environment variable name. Two
    names that would share an environment variable (``a-b`` and ``a_b``) are refused.
    """
    if isinstance(names, str) or not isinstance(names, Iterable):
        raise RoleError("roles must be a list of role names")
    roles = tuple(names)
    if not roles:
        raise RoleError("roles must not be empty; leave it out for a single-credential run")
    seen: dict[str, str] = {}
    for name in roles:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise RoleError(
                f"invalid role name {name!r}: use lowercase letters, digits, '-' and '_', "
                "starting with a letter, at most 32 characters"
            )
        if name in ("roles", "pages", "screenshots"):
            raise RoleError(f"{name!r} is reserved (it is a name Healix uses in the output folder)")
        clash = seen.get(env_suffix(name))
        if clash is not None:
            what = "is listed twice" if clash == name else f"and {clash!r} share {env_suffix(name)}"
            raise RoleError(f"role {name!r} {what}; each role needs its own credentials")
        seen[env_suffix(name)] = name
    return roles
