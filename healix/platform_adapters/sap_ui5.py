"""SAP UI5 / SAPUI5 / OpenUI5.

When ``window.sap`` is present, an element that belongs to a UI5 control gets the control's id,
type and whether the element is the control's root:

    {"platform": "sap_ui5", "control_id": "__xmlview0--saveButton",
     "control_type": "sap.m.Button", "is_control_root": true}

UI5 marks each control's root DOM element with ``data-sap-ui="<control id>"``; the control is then
looked up in the UI5 registry — ``sap.ui.getCore().byId()``, or ``Element.getElementById()`` on UI5
versions where ``getCore`` is gone. An element that is not inside a control gets no signal.
"""

from __future__ import annotations

from healix.platform_adapters.base import PlatformAdapter

NAME = "sap_ui5"

DETECT_JS = "!!(window.sap && window.sap.ui)"

SIGNAL_JS = r"""(el) => {
  const ui = window.sap.ui;
  let byId = null;
  if (typeof ui.getCore === 'function') {
    const core = ui.getCore();
    if (core && typeof core.byId === 'function') byId = (id) => core.byId(id);
  }
  if (!byId && typeof ui.require === 'function') {
    const Element = ui.require('sap/ui/core/Element');
    if (Element && typeof Element.getElementById === 'function') byId = (id) => Element.getElementById(id);
  }
  if (!byId) return null;
  const root = el.hasAttribute('data-sap-ui') ? el : el.closest('[data-sap-ui]');
  if (!root) return null;
  const control = byId(root.getAttribute('data-sap-ui'));
  if (!control) return null;
  return {
    control_id: control.getId(),
    control_type: control.getMetadata().getName(),
    is_control_root: root === el,
  };
}"""

ADAPTER = PlatformAdapter(NAME, DETECT_JS, SIGNAL_JS)
