"""Small pages that look like the platforms' runtimes, for the adapter tests.

The stubs have the shape of the real APIs (``sap.ui.getCore().byId``, ``data-sap-ui``, custom
elements in shadow roots, ``$A``). The SAP adapter was also checked by hand against a real OpenUI5
runtime; that needs the network, so it is not part of the suite.
"""

SAP_CONTROLS = """
const controls = {app: 'sap.m.App', save: 'sap.m.Button', name: 'sap.m.Input'};
const control = (id) => controls[id]
  ? {getId: () => id, getMetadata: () => ({getName: () => controls[id]})}
  : undefined;
"""

BODY = """
<div id="app" data-sap-ui="app">
  <button id="save" data-sap-ui="save"><span id="save-inner">Save</span></button>
  <div id="name" data-sap-ui="name"><input id="name-inner"></div>
  <div id="orphan" data-sap-ui="not-a-control"><span id="orphan-child">x</span></div>
</div>
<p id="plain">not in a control</p>
"""

# UI5 1.x: the registry is sap.ui.getCore().byId
SAP_UI5_CORE = f"<body>{BODY}<script>{SAP_CONTROLS}window.sap = {{ui: {{getCore: () => ({{byId: control}})}}}};</script></body>"

# UI5 2.x: sap.ui.getCore is gone; the registry is sap/ui/core/Element
SAP_UI5_ELEMENT = (
    f"<body>{BODY}<script>{SAP_CONTROLS}window.sap = {{ui: {{require: (module) =>"
    " module === 'sap/ui/core/Element' ? {getElementById: control} : undefined}};</script></body>"
)

# `window.sap` exists (some other SAP library), but there is no UI5 registry to ask.
SAP_WITHOUT_UI5_REGISTRY = f"<body>{BODY}<script>window.sap = {{ui: {{}}}};</script></body>"

# A registry that throws for every lookup.
SAP_BROKEN_REGISTRY = f"<body>{BODY}<script>window.sap = {{ui: {{getCore: () => ({{byId: () => {{ throw new Error('boom'); }}}})}}}};</script></body>"

# The same markup and no `window.sap`.
NOT_SAP = f"<body>{BODY}</body>"

_LWC_BODY = """
<c-parent id="host"></c-parent>
<div id="light" data-aura-class="cCmp"></div>
<lightning-badge id="top-level"></lightning-badge>
<p id="plain">plain</p>
<script>
const root = document.getElementById('host').attachShadow({mode: 'open'});
root.innerHTML = '<lightning-input id="field" data-aura-rendered-by="1:0" data-other="x"><input id="inner"></lightning-input><div id="in-shadow"></div>';
</script>
"""

SALESFORCE_WITH_AURA_GLOBAL = f"<body><script>window.$A = {{}};</script>{_LWC_BODY}</body>"

# No `$A` or `Aura`: the `data-aura-*` markers alone show it is Aura-rendered.
SALESFORCE_BY_MARKER_ONLY = f"<body>{_LWC_BODY}</body>"

# Custom elements and shadow roots, but nothing that says Salesforce.
NOT_SALESFORCE = (
    "<body><c-parent id='host'></c-parent><lightning-badge id='top-level'></lightning-badge>"
    "<script>document.getElementById('host').attachShadow({mode: 'open'}).innerHTML = '<div id=\"in-shadow\"></div>';</script></body>"
)
