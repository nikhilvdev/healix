"""What a platform adapter is.

An adapter adds one extra signal — ``platform_signal`` — to elements on pages built with a
particular UI platform, for facts that no generic rule can see (a SAP UI5 control id, a Salesforce
component tag). It only ever *adds*: extraction, healing and classification all work from the
generic pipeline alone, and an adapter that does not fire, or fails, changes nothing.

An adapter is two pieces of in-page JavaScript, run inside each frame by the driver's collector
(so it works the same on every backend):

* ``detect_js`` — an expression that is true when the platform is present in that frame.
* ``signal_js`` — a function expression ``(element) => object | null``, tried on each element
  only in frames where detection passed. A non-null result becomes the element's
  ``platform_signal``, with ``"platform": <name>`` added.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformAdapter:
    name: str
    detect_js: str
    signal_js: str
