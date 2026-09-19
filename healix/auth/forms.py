"""Finding the pieces of a login flow on a page — pure functions over ``Element`` lists.

Nothing here touches a browser. ``find_login_form`` locates the username, password and
submit controls (and any single-sign-on buttons); ``detect_mfa`` recognises a second-factor
challenge the crawler cannot complete; ``login_error_visible`` recognises a rejected login.
The handler acts on the ``Element`` objects returned, through ``Driver.write``/``click``,
so forms inside iframes and shadow roots work like any other.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from healix.driver.base import Element

_TEXT_INPUT_TYPES = frozenset({"", "text", "email", "tel"})
_USERNAME_HINT = re.compile(r"user(name)?|e-?mail|login|identifier|account|phone|\bid\b")
_NOT_A_USERNAME = re.compile(r"search|\bq\b|query|captcha|coupon|promo|newsletter")
_SUBMIT_TEXT = re.compile(r"\b(sign ?in|log ?in|continue|next|submit|go|enter)\b")
_SSO_BUTTON = re.compile(
    r"(sign|log) ?in with|continue with|single sign-?on|\bsso\b|"
    r"use (your )?(google|microsoft|okta|github|apple)"
)

_OTP_INPUT = re.compile(
    r"one-time-code|\botp\b|totp|\bmfa\b|\b2fa\b|two[-_ ]?factor|verification[-_ ]?code|"
    r"security[-_ ]?code|authenticator|passcode|one[-_ ]?time|sms[-_ ]?code"
)
_MFA_TEXT = re.compile(
    r"two[- ]factor|2[- ]step|two[- ]step|verification code|enter (the|your) (6-digit )?code|"
    r"authenticator app|one[- ]time (code|password)|check your (phone|email)|"
    r"we (sent|texted|emailed) (you )?a code"
)
_ERROR_TEXT = re.compile(
    r"(invalid|incorrect|wrong|unknown) (user ?name|password|credentials|e-?mail|login)|"
    r"(login|sign[- ]?in|authentication) (failed|error|unsuccessful)|"
    r"(couldn't|could not|can't|cannot) (find|sign|log)|not recognized|doesn't match|"
    r"does not match|try again|account (is )?locked|too many (failed )?(attempts|tries)|"
    r"access denied|unable to (sign|log)"
)


@dataclass(frozen=True)
class LoginForm:
    """The controls of one login step. Any of them may be absent."""

    username: Element | None = None
    password: Element | None = None
    submit: Element | None = None
    sso: list[Element] = field(default_factory=list)


def _usable(element: Element) -> bool:
    return bool(element.computed.get("visible", True)) and bool(
        element.computed.get("enabled", True)
    )


def _type(element: Element) -> str:
    return element.attributes.get("type", "").lower()


def _blob(element: Element) -> str:
    attrs = element.attributes
    parts = (
        element.id,
        element.name,
        *element.classes,
        attrs.get("placeholder"),
        attrs.get("aria-label"),
        attrs.get("autocomplete"),
        attrs.get("title"),
        attrs.get("data-testid"),
    )
    return " ".join(p for p in parts if p).lower()


def _text(element: Element) -> str:
    text = element.text_content or ""
    if not text and element.tag == "input" and _type(element) in ("submit", "button", "image"):
        text = element.attributes.get("value", "")
    return " ".join(text.lower().split())


def _inside(element: Element, container: Element) -> bool:
    return (
        element.iframe_path == container.iframe_path
        and bool(container.css_selector)
        and (element.css_selector or "").startswith(f"{container.css_selector} ")
    )


def find_login_form(elements: Sequence[Element]) -> LoginForm:
    """Locate the login controls among the visible, enabled elements of a page.

    Exactly one password field is required to identify a password step — a page with
    several (registration, change-password) yields no password, so nothing is filled.
    The username is the text-like input closest before the password (or the best hint match);
    the submit button is preferably one inside the same ``<form>``.
    """
    order = {id(e): i for i, e in enumerate(elements)}
    usable = [e for e in elements if _usable(e)]

    sso = [
        e
        for e in usable
        if (e.tag == "button" or e.tag == "a" or e.attributes.get("role") == "button")
        and (_SSO_BUTTON.search(_text(e)) or _SSO_BUTTON.search(_blob(e)))
    ]

    passwords = [e for e in usable if e.tag == "input" and _type(e) == "password"]
    password = passwords[0] if len(passwords) == 1 else None

    scope = [e for e in usable if password is None or e.iframe_path == password.iframe_path]
    if password is not None:
        forms = [f for f in scope if f.tag == "form" and _inside(password, f)]
        container = max(forms, key=lambda f: len(f.css_selector or ""), default=None)
        members = [e for e in scope if container is None or _inside(e, container)]
    else:
        members = scope

    candidates = [
        e
        for e in members
        if e.tag == "input"
        and _type(e) in _TEXT_INPUT_TYPES
        and not _NOT_A_USERNAME.search(_blob(e))
    ]
    if password is not None:
        candidates = [e for e in candidates if order[id(e)] < order[id(password)]]
    hinted = [e for e in candidates if _USERNAME_HINT.search(_blob(e))]
    pool = hinted or candidates
    username = pool[-1] if pool else None

    def is_submit(e: Element) -> bool:
        if e.tag == "input" and _type(e) in ("submit", "image"):
            return True
        if e.tag == "button":
            return _type(e) == "submit" or (not _type(e) and bool(_SUBMIT_TEXT.search(_text(e))))
        return False

    submits = [e for e in members if is_submit(e)]
    if password is not None:
        after = [e for e in submits if order[id(e)] > order[id(password)]]
        submits = after or submits
    labelled = [e for e in submits if _SUBMIT_TEXT.search(_text(e))]
    ranked = labelled or submits
    submit = ranked[0] if ranked else None

    return LoginForm(username=username, password=password, submit=submit, sso=sso)


def detect_mfa(elements: Sequence[Element]) -> bool:
    """Whether the page is a second-factor challenge (a one-time-code prompt).

    True for a visible one-time-code input, or for second-factor wording alongside a
    visible text input.
    """
    visible = [e for e in elements if _usable(e)]
    inputs = [
        e
        for e in visible
        if e.tag == "input" and _type(e) in ("", "text", "tel", "number", "password")
    ]
    if any(_OTP_INPUT.search(_blob(e)) for e in inputs):
        return True
    has_cue = any(_MFA_TEXT.search(_text(e)) for e in visible if _text(e))
    return has_cue and any(_type(e) != "password" for e in inputs)


def login_error_visible(elements: Sequence[Element]) -> bool:
    """Whether the page shows a "wrong password"-style message."""
    return any(
        _ERROR_TEXT.search(_text(e))
        for e in elements
        if _usable(e) and _text(e) and len(_text(e)) < 250
    )
