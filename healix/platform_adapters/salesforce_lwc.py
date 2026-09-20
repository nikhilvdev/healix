"""Salesforce Lightning (Aura and Lightning Web Components).

When the Aura globals (``$A`` / ``Aura``) or Aura's rendering markers are present, an element gets
the tag of the Lightning component it sits in, and any ``data-aura-*`` attributes it carries:

    {"platform": "salesforce_lwc", "component": "lightning-input",
     "is_component_host": false, "aura_attributes": {"data-aura-rendered-by": "12:0"}}

``component`` is the element's own tag when that is a component (a custom element, whose tag has
a hyphen) and otherwise its nearest enclosing shadow host. Elements that are in no component and
have no ``data-aura-*`` attribute get no signal.
"""

from __future__ import annotations

from healix.platform_adapters.base import PlatformAdapter

NAME = "salesforce_lwc"

DETECT_JS = (
    "!!(window.$A || window.Aura"
    " || document.querySelector('[data-aura-rendered-by], [data-aura-class]'))"
)

SIGNAL_JS = r"""(el) => {
  const aura = {};
  for (const attribute of el.attributes) {
    if (attribute.name.startsWith('data-aura-')) aura[attribute.name] = attribute.value;
  }
  const isComponent = el.localName.includes('-');
  const root = el.getRootNode();
  const host = isComponent ? el : (root && root.host && root.host.localName.includes('-') ? root.host : null);
  if (!host && !Object.keys(aura).length) return null;
  return {
    component: host ? host.localName : null,
    is_component_host: isComponent,
    aura_attributes: aura,
  };
}"""

ADAPTER = PlatformAdapter(NAME, DETECT_JS, SIGNAL_JS)
