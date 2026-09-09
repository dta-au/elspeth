/* @ds-bundle: {"format":3,"namespace":"ELSPETHDesignSystem_85edbb","components":[{"name":"ChatBubble","sourcePath":"components/composer/ChatBubble.jsx"},{"name":"PluginCard","sourcePath":"components/composer/PluginCard.jsx"},{"name":"WordMark","sourcePath":"components/composer/WordMark.jsx"},{"name":"AlertBanner","sourcePath":"components/core/AlertBanner.jsx"},{"name":"Button","sourcePath":"components/core/Button.jsx"},{"name":"Card","sourcePath":"components/core/Card.jsx"},{"name":"CardHeader","sourcePath":"components/core/Card.jsx"},{"name":"StatusBadge","sourcePath":"components/core/StatusBadge.jsx"},{"name":"Tabs","sourcePath":"components/core/Tabs.jsx"},{"name":"TypeBadge","sourcePath":"components/core/TypeBadge.jsx"},{"name":"Input","sourcePath":"components/forms/Input.jsx"},{"name":"Textarea","sourcePath":"components/forms/Textarea.jsx"}],"sourceHashes":{"components/composer/ChatBubble.jsx":"f0176d6d7055","components/composer/PluginCard.jsx":"301063997ef4","components/composer/WordMark.jsx":"65daf5c47656","components/core/AlertBanner.jsx":"41a3b47b341d","components/core/Button.jsx":"c795cfe7707f","components/core/Card.jsx":"6e3fcf89f5ef","components/core/StatusBadge.jsx":"dfdc327843d5","components/core/Tabs.jsx":"4d95381497eb","components/core/TypeBadge.jsx":"23b33bcc87e2","components/forms/Input.jsx":"2c2924d3465d","components/forms/Textarea.jsx":"eaad8a1f7e37","ui_kits/composer/CatalogDrawer.jsx":"35e0637f6585","ui_kits/composer/ComposerShell.jsx":"2aca6788991e","ui_kits/composer/LoginScreen.jsx":"cf9446c1c9ce","ui_kits/composer/PipelineGraph.jsx":"d72889de0ddb","ui_kits/composer/app.jsx":"27ac50309864","ui_kits/composer/data.js":"75ae9919d1e1","ui_kits/website/site.js":"fa55df86ba72"},"inlinedExternals":[],"unexposedExports":[]} */

(() => {

const __ds_ns = (window.ELSPETHDesignSystem_85edbb = window.ELSPETHDesignSystem_85edbb || {});

const __ds_scope = {};

(__ds_ns.__errors = __ds_ns.__errors || []);

// components/composer/ChatBubble.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Chat message bubble for the ELSPETH composer conversation. `role` selects
 * the user (right, green tint), assistant (left, white tint + 2px left accent),
 * or system (centred, italic, muted) treatment.
 */
function ChatBubble({
  role = "assistant",
  className = "",
  children,
  ...rest
}) {
  const rowJustify = role === "user" ? "flex-end" : role === "system" ? "center" : "flex-start";
  const bubbleBase = {
    padding: "var(--space-md) var(--space-lg)",
    borderRadius: "var(--radius-lg)",
    lineHeight: "var(--line-height-snug)",
    fontSize: "var(--font-size-base)",
    color: "var(--color-text)",
    wordBreak: "break-word",
    maxWidth: "min(80%, 68ch)"
  };
  const byRole = {
    user: {
      background: "var(--color-bubble-user)",
      border: "1px solid var(--color-bubble-user-border)",
      whiteSpace: "pre-wrap"
    },
    assistant: {
      background: "var(--color-bubble-assistant)",
      border: "1px solid var(--color-bubble-assistant-border)",
      borderLeft: "2px solid var(--color-border-strong)"
    },
    system: {
      background: "var(--color-bubble-system)",
      color: "var(--color-text-muted)",
      fontStyle: "italic",
      fontSize: "var(--font-size-sm)",
      textAlign: "center",
      maxWidth: "100%",
      width: "100%",
      border: "none"
    }
  };
  return /*#__PURE__*/React.createElement("div", _extends({
    className: className,
    style: {
      display: "flex",
      justifyContent: rowJustify,
      padding: "var(--space-xs) var(--space-lg)"
    }
  }, rest), /*#__PURE__*/React.createElement("div", {
    style: {
      ...bubbleBase,
      ...byRole[role]
    }
  }, children));
}
Object.assign(__ds_scope, { ChatBubble });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/composer/ChatBubble.jsx", error: String((e && e.message) || e) }); }

// components/composer/WordMark.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * The ELSPETH wordmark. Renders the brand as live text in JetBrains Mono 700,
 * uppercase, 0.18em tracking — never an image. `size` sets the font size.
 */
function WordMark({
  size = 13,
  as: Tag = "span",
  className = "",
  style,
  ...rest
}) {
  return /*#__PURE__*/React.createElement(Tag, _extends({
    className: className,
    style: {
      fontFamily: "var(--font-mono)",
      fontWeight: 700,
      fontSize: typeof size === "number" ? `${size}px` : size,
      textTransform: "uppercase",
      letterSpacing: "0.18em",
      color: "var(--color-text)",
      ...style
    }
  }, rest), "ELSPETH");
}
Object.assign(__ds_scope, { WordMark });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/composer/WordMark.jsx", error: String((e && e.message) || e) }); }

// components/core/AlertBanner.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Inline alert / status banner. Tone maps to ELSPETH semantic colours. Used for
 * backend-unavailable, service-unavailable, redirect toasts, etc. `action`
 * renders a right-aligned button (e.g. Retry, Configure API keys).
 */
function AlertBanner({
  tone = "error",
  action = null,
  role,
  className = "",
  children,
  ...rest
}) {
  const toneClass = tone === "info" ? "alert-banner--info" : tone === "warning" ? "alert-banner--warning" : tone === "success" ? "alert-banner--success" : "";
  const cls = ["alert-banner", toneClass, className].filter(Boolean).join(" ");
  const resolvedRole = role ?? (tone === "error" ? "alert" : "status");
  return /*#__PURE__*/React.createElement("div", _extends({
    className: cls,
    role: resolvedRole
  }, rest), /*#__PURE__*/React.createElement("span", null, children), action ? /*#__PURE__*/React.createElement("span", {
    style: {
      flexShrink: 0
    }
  }, action) : null);
}
Object.assign(__ds_scope, { AlertBanner });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/AlertBanner.jsx", error: String((e && e.message) || e) }); }

// components/core/Button.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * ELSPETH button. Composes the design-system `.btn` family.
 * Variants: primary (green CTA), danger (destructive), secondary (default
 * elevated surface), ghost (transparent). `compact` switches to the 36px
 * chrome-row size used in headers and dense toolbars.
 */
function Button({
  variant = "secondary",
  compact = false,
  type = "button",
  disabled = false,
  iconLeft = null,
  iconRight = null,
  className = "",
  children,
  ...rest
}) {
  const base = compact ? "btn-compact" : "btn";
  const variantClass = variant === "primary" ? "btn-primary" : variant === "danger" ? "btn-danger" : variant === "ghost" ? "btn-ghost" : "";
  const cls = [base, variantClass, className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("button", _extends({
    type: type,
    className: cls,
    disabled: disabled
  }, rest), iconLeft, children, iconRight);
}
Object.assign(__ds_scope, { Button });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Button.jsx", error: String((e && e.message) || e) }); }

// components/core/Card.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Surface card. `paper` switches to the warm-neutral inspection family used by
 * right-rail and modal panels. `pad` toggles default padding off for media.
 */
function Card({
  paper = false,
  pad = true,
  className = "",
  style,
  children,
  ...rest
}) {
  const cls = ["card", paper ? "card-paper" : "", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("div", _extends({
    className: cls,
    style: {
      ...(pad ? null : {
        padding: 0
      }),
      ...style
    }
  }, rest), children);
}

/** Optional header row for a Card: title + right-aligned actions slot. */
function CardHeader({
  title,
  actions = null,
  eyebrow = null
}) {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "flex-start",
      justifyContent: "space-between",
      gap: "var(--space-sm)",
      marginBottom: "var(--space-md)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      minWidth: 0
    }
  }, eyebrow ? /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: "var(--font-size-3xs)",
      fontWeight: 700,
      textTransform: "uppercase",
      letterSpacing: "0.08em",
      color: "var(--color-text-muted)",
      marginBottom: 2
    }
  }, eyebrow) : null, /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: "var(--font-size-base)",
      fontWeight: 700,
      color: "var(--color-text)"
    }
  }, title)), actions);
}
Object.assign(__ds_scope, { Card, CardHeader });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Card.jsx", error: String((e && e.message) || e) }); }

// components/core/StatusBadge.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const GLYPH = {
  completed: null,
  completed_with_failures: "⚠",
  empty: "∅"
};

/**
 * Run-lifecycle status badge. Maps an ELSPETH terminal/lifecycle status to its
 * colour and (where used) functional glyph. completed_with_failures reuses the
 * teal "completed" colour and signals caveats with ⚠; empty uses a neutral
 * grey with ∅.
 */
function StatusBadge({
  status = "pending",
  className = "",
  children,
  ...rest
}) {
  const colorKey = status === "completed_with_failures" ? "completed" : status === "cancelling" ? "cancelled" : status;
  const cls = ["status-badge", `status-badge-${colorKey}`, className].filter(Boolean).join(" ");
  const glyph = GLYPH[status];
  return /*#__PURE__*/React.createElement("span", _extends({
    className: cls
  }, rest), glyph ? /*#__PURE__*/React.createElement("span", {
    "aria-hidden": "true"
  }, glyph) : null, children ?? status);
}
Object.assign(__ds_scope, { StatusBadge });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/StatusBadge.jsx", error: String((e && e.message) || e) }); }

// components/core/Tabs.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Underline tab strip. Controlled via `value`/`onChange`. Each tab is
 * `{ id, label, count? }`; an optional count renders as a small pill.
 */
function Tabs({
  tabs = [],
  value,
  onChange,
  className = "",
  ...rest
}) {
  const cls = ["tab-strip", className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("div", _extends({
    className: cls,
    role: "tablist"
  }, rest), tabs.map(t => {
    const active = t.id === value;
    return /*#__PURE__*/React.createElement("button", {
      key: t.id,
      role: "tab",
      "aria-selected": active,
      className: ["tab-strip-tab", active ? "tab-strip-tab-active" : ""].filter(Boolean).join(" "),
      onClick: () => onChange?.(t.id)
    }, t.label, typeof t.count === "number" ? /*#__PURE__*/React.createElement("span", {
      style: {
        marginLeft: 6,
        fontSize: "var(--font-size-3xs)",
        padding: "1px 5px",
        borderRadius: "var(--radius-lg)",
        fontWeight: 600,
        background: active ? "var(--color-accent)" : "var(--color-surface-elevated)",
        color: active ? "var(--color-text-inverse)" : "var(--color-text-muted)"
      }
    }, t.count) : null);
  }));
}
Object.assign(__ds_scope, { Tabs });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/Tabs.jsx", error: String((e && e.message) || e) }); }

// components/core/TypeBadge.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
const TYPES = ["source", "transform", "gate", "sink", "aggregation", "coalesce"];

/**
 * Component-type badge — the fixed colour vocabulary for the six ELSPETH
 * pipeline primitives. Mono typeface, uppercase, used in catalog cards,
 * graph nodes, and validation messages.
 */
function TypeBadge({
  type = "source",
  className = "",
  children,
  ...rest
}) {
  const t = TYPES.includes(type) ? type : "source";
  const cls = ["type-badge", `type-badge-${t}`, className].filter(Boolean).join(" ");
  return /*#__PURE__*/React.createElement("span", _extends({
    className: cls
  }, rest), children ?? t);
}
Object.assign(__ds_scope, { TypeBadge });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/core/TypeBadge.jsx", error: String((e && e.message) || e) }); }

// components/composer/PluginCard.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Plugin catalog card — one entry in the ELSPETH plugin catalog drawer. Shows
 * the plugin name, its component type, a clamped description, and a strip of
 * audit-characteristic chips (positive / attention / informational).
 */
function PluginCard({
  name,
  type = "source",
  kind,
  description,
  audit = [],
  onTry,
  ...rest
}) {
  return /*#__PURE__*/React.createElement("div", _extends({
    style: {
      padding: "var(--space-md)",
      borderBottom: "1px solid var(--color-border)",
      background: "var(--color-surface)"
    }
  }, rest), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "baseline",
      justifyContent: "space-between",
      gap: "var(--space-sm)"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontWeight: 700,
      fontSize: "var(--font-size-sm)",
      color: "var(--color-text)"
    }
  }, name), /*#__PURE__*/React.createElement(__ds_scope.TypeBadge, {
    type: type
  }, kind ?? type)), description ? /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 2,
      fontSize: "var(--font-size-xs)",
      color: "var(--color-text-muted)",
      lineHeight: 1.35
    }
  }, description) : null, audit.length ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexWrap: "wrap",
      gap: 4,
      marginTop: "var(--space-xs)"
    }
  }, audit.map((a, i) => /*#__PURE__*/React.createElement(AuditChip, {
    key: i,
    tone: a.tone
  }, a.label))) : null, onTry ? /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      gap: "var(--space-xs)",
      marginTop: "var(--space-sm)"
    }
  }, /*#__PURE__*/React.createElement("button", {
    className: "btn-compact",
    style: {
      minHeight: 30,
      fontSize: "var(--font-size-xs)"
    },
    onClick: onTry
  }, "Try in composer")) : null);
}
function AuditChip({
  tone = "informational",
  children
}) {
  const toneStyle = {
    positive: {
      color: "var(--color-success)",
      borderColor: "var(--color-success-border)",
      background: "var(--color-success-bg)"
    },
    attention: {
      color: "var(--color-warning)",
      borderColor: "var(--color-warning-border)",
      background: "var(--color-warning-bg)"
    },
    informational: {
      color: "var(--color-info)",
      borderColor: "var(--color-info-border)",
      background: "var(--color-info-bg)"
    }
  }[tone] || {};
  return /*#__PURE__*/React.createElement("span", {
    style: {
      display: "inline-flex",
      alignItems: "center",
      minHeight: 22,
      padding: "2px 6px",
      border: "1px solid var(--color-border)",
      borderRadius: "var(--radius-sm)",
      fontSize: "var(--font-size-3xs)",
      fontWeight: 650,
      lineHeight: 1.2,
      ...toneStyle
    }
  }, children);
}
Object.assign(__ds_scope, { PluginCard });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/composer/PluginCard.jsx", error: String((e && e.message) || e) }); }

// components/forms/Input.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Text input. Composes `.input`. `mono` switches to JetBrains Mono for secret
 * names, paths, and other forensic-register values. Pair with `label`/`hint`
 * or use the bare control inside your own field layout.
 */
function Input({
  label,
  hint,
  mono = false,
  id,
  className = "",
  ...rest
}) {
  const inputId = id || (label ? `inp-${Math.random().toString(36).slice(2, 8)}` : undefined);
  const cls = ["input", mono ? "input-mono" : "", className].filter(Boolean).join(" ");
  const control = /*#__PURE__*/React.createElement("input", _extends({
    id: inputId,
    className: cls
  }, rest));
  if (!label && !hint) return control;
  return /*#__PURE__*/React.createElement("div", null, label ? /*#__PURE__*/React.createElement("label", {
    className: "field-label",
    htmlFor: inputId
  }, label) : null, control, hint ? /*#__PURE__*/React.createElement("div", {
    className: "field-hint"
  }, hint) : null);
}
Object.assign(__ds_scope, { Input });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Input.jsx", error: String((e && e.message) || e) }); }

// components/forms/Textarea.jsx
try { (() => {
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
/**
 * Multiline text input. Composes `.textarea` (vertical resize). Optional
 * label + hint. Used for the chat composer input, prompt editing, and the
 * "I meant…" amend forms.
 */
function Textarea({
  label,
  hint,
  id,
  className = "",
  rows = 3,
  ...rest
}) {
  const taId = id || (label ? `ta-${Math.random().toString(36).slice(2, 8)}` : undefined);
  const cls = ["textarea", className].filter(Boolean).join(" ");
  const control = /*#__PURE__*/React.createElement("textarea", _extends({
    id: taId,
    className: cls,
    rows: rows
  }, rest));
  if (!label && !hint) return control;
  return /*#__PURE__*/React.createElement("div", null, label ? /*#__PURE__*/React.createElement("label", {
    className: "field-label",
    htmlFor: taId
  }, label) : null, control, hint ? /*#__PURE__*/React.createElement("div", {
    className: "field-hint"
  }, hint) : null);
}
Object.assign(__ds_scope, { Textarea });
})(); } catch (e) { __ds_ns.__errors.push({ path: "components/forms/Textarea.jsx", error: String((e && e.message) || e) }); }

// ui_kits/composer/CatalogDrawer.jsx
try { (() => {
// CatalogDrawer.jsx — the plugin catalog drawer that slides in from the right.
function CatalogDrawer({
  open,
  onClose,
  onTry
}) {
  const {
    PluginCard,
    Tabs,
    Input
  } = window.ELSPETHDesignSystem_85edbb;
  const cat = window.ELSPETH_KIT.catalog;
  const [tab, setTab] = React.useState("sources");
  const [q, setQ] = React.useState("");
  if (!open) return null;
  const list = (cat[tab] || []).filter(p => p.name.toLowerCase().includes(q.toLowerCase()) || (p.kind || "").includes(q.toLowerCase()));
  return /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("div", {
    onClick: onClose,
    style: {
      position: "absolute",
      inset: 0,
      background: "rgba(0,0,0,0.3)",
      zIndex: 40
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      position: "absolute",
      top: 0,
      right: 0,
      bottom: 0,
      width: "min(440px, calc(100% - 24px))",
      zIndex: 41,
      background: "var(--color-surface)",
      borderLeft: "1px solid var(--color-border)",
      display: "flex",
      flexDirection: "column"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "flex-start",
      justifyContent: "space-between",
      gap: 8,
      padding: 16,
      borderBottom: "1px solid var(--color-border)"
    }
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 10,
      fontWeight: 700,
      textTransform: "uppercase",
      letterSpacing: ".08em",
      color: "var(--color-text-muted)"
    }
  }, "Plugin catalog"), /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 16,
      fontWeight: 700,
      color: "var(--color-text)"
    }
  }, "Sources, transforms & sinks"), /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 12,
      color: "var(--color-text-muted)",
      marginTop: 2
    }
  }, "Discovered via pluggy. Each shows its audit characteristics.")), /*#__PURE__*/React.createElement("button", {
    className: "btn-compact",
    onClick: onClose,
    "aria-label": "Close catalog",
    style: {
      minWidth: 36
    }
  }, "\xD7")), /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "8px 16px",
      borderBottom: "1px solid var(--color-border)"
    }
  }, /*#__PURE__*/React.createElement(Input, {
    mono: true,
    value: q,
    onChange: e => setQ(e.target.value),
    placeholder: "Search plugins\u2026"
  })), /*#__PURE__*/React.createElement(Tabs, {
    value: tab,
    onChange: setTab,
    tabs: [{
      id: "sources",
      label: "Sources",
      count: cat.sources.length
    }, {
      id: "transforms",
      label: "Transforms",
      count: cat.transforms.length
    }, {
      id: "sinks",
      label: "Sinks",
      count: cat.sinks.length
    }]
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      overflowY: "auto"
    }
  }, list.length === 0 ? /*#__PURE__*/React.createElement("div", {
    style: {
      padding: 16,
      fontSize: 12,
      color: "var(--color-text-muted)"
    }
  }, "No plugins match \u201C", q, "\u201D.") : list.map(p => /*#__PURE__*/React.createElement(PluginCard, {
    key: p.kind + p.name,
    name: p.name,
    type: p.type,
    kind: p.kind,
    description: p.description,
    audit: p.audit,
    onTry: () => onTry(p)
  })))));
}
Object.assign(window, {
  CatalogDrawer
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/composer/CatalogDrawer.jsx", error: String((e && e.message) || e) }); }

// ui_kits/composer/ComposerShell.jsx
try { (() => {
// ComposerShell.jsx — header (navy) + chat panel (teal) + side rail (paper).
// Icon isolates lucide's DOM mutation from React. lucide replaces the
// <i data-lucide> with an <svg> behind React's back; if React ever reconciles
// that node it crashes (removeChild on a detached node). So we render a span
// React treats as EMPTY (no JSX children) and own its inner DOM via a ref —
// React never touches the lucide-mutated node, so name changes are safe.
function Icon({
  name,
  size = 18,
  color
}) {
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!ref.current) return;
    ref.current.innerHTML = "";
    const i = document.createElement("i");
    i.setAttribute("data-lucide", name);
    ref.current.appendChild(i);
    if (window.lucide) window.lucide.createIcons();
  }, [name, size, color]);
  return /*#__PURE__*/React.createElement("span", {
    ref: ref,
    style: {
      display: "inline-flex",
      width: size,
      height: size,
      color: color || "inherit"
    }
  });
}

/* ── Header ──────────────────────────────────────────────────────────────── */
function ComposerHeader({
  sessionTitle,
  theme,
  onToggleTheme,
  onSignOut
}) {
  const {
    WordMark
  } = window.ELSPETHDesignSystem_85edbb;
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      height: 40,
      padding: "0 12px",
      borderBottom: "1px solid var(--color-border)",
      background: "var(--color-surface-nav)",
      flexShrink: 0
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: 12
    }
  }, /*#__PURE__*/React.createElement(WordMark, {
    size: 13
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      width: 1,
      height: 20,
      background: "var(--color-border)"
    }
  }), /*#__PURE__*/React.createElement("button", {
    className: "btn-compact",
    style: {
      gap: 8
    }
  }, sessionTitle, " ", /*#__PURE__*/React.createElement(Icon, {
    name: "chevron-down",
    size: 14
  }))), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: 8
    }
  }, /*#__PURE__*/React.createElement("button", {
    className: "btn-compact",
    onClick: onToggleTheme,
    "aria-label": "Toggle theme",
    title: "Toggle theme"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: theme === "dark" ? "sun" : "moon",
    size: 16
  })), /*#__PURE__*/React.createElement("button", {
    className: "btn-compact",
    "aria-label": "Settings"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "settings",
    size: 16
  })), /*#__PURE__*/React.createElement("button", {
    className: "btn-compact",
    onClick: onSignOut,
    style: {
      gap: 8
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "user",
    size: 14
  }), " Demo User")));
}

/* ── Composing indicator ─────────────────────────────────────────────────── */
function ComposingIndicator() {
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      justifyContent: "flex-start",
      padding: "4px 16px"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "10px 14px",
      borderRadius: "var(--radius-md)",
      background: "var(--color-bubble-assistant)",
      border: "1px solid var(--color-bubble-assistant-border)",
      display: "flex",
      gap: 8,
      alignItems: "center"
    }
  }, /*#__PURE__*/React.createElement("span", {
    className: "composing-dot"
  }), /*#__PURE__*/React.createElement("span", {
    className: "composing-dot"
  }), /*#__PURE__*/React.createElement("span", {
    className: "composing-dot"
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: 12,
      color: "var(--color-text-muted)"
    }
  }, "Working\u2026")));
}

/* ── Tool-call card inside an assistant turn ─────────────────────────────── */
function ToolCallCard({
  tool,
  summary,
  state
}) {
  const accent = state === "committed" ? "var(--color-success)" : state === "rejected" ? "var(--color-error)" : "var(--color-warning)";
  return /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 8,
      padding: 8,
      border: "1px solid var(--color-border-strong)",
      borderLeft: `4px solid ${accent}`,
      borderRadius: "var(--radius-md)",
      background: "var(--color-surface-elevated)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      justifyContent: "space-between",
      gap: 8,
      alignItems: "center"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontFamily: "var(--font-mono)",
      fontSize: 12,
      color: "var(--color-text)"
    }
  }, tool), /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: 10,
      textTransform: "uppercase",
      letterSpacing: ".06em",
      color: accent,
      fontWeight: 700
    }
  }, state)), /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 6,
      fontSize: 13,
      color: "var(--color-text-secondary)"
    }
  }, summary));
}

/* ── Empty-state template cards ──────────────────────────────────────────── */
function TemplateCards({
  onPick
}) {
  const items = [{
    icon: "scale",
    title: "Tender evaluation",
    sense: "CSV of submissions",
    decide: "LLM + safety gate",
    act: "Results, review queue"
  }, {
    icon: "file-search",
    title: "Document QA",
    sense: "PDF / text blobs",
    decide: "Extraction, rubric checks",
    act: "Annotated outputs"
  }, {
    icon: "shield-alert",
    title: "Content moderation",
    sense: "User submissions",
    decide: "Safety classifier",
    act: "Published, review, rejected"
  }, {
    icon: "activity",
    title: "Threshold monitoring",
    sense: "Sensor feed",
    decide: "Threshold + anomaly",
    act: "Log, warning, alert"
  }];
  return /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "24px 28px",
      maxWidth: 760,
      margin: "0 auto"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      textAlign: "center",
      marginBottom: 22
    }
  }, /*#__PURE__*/React.createElement("h2", {
    style: {
      margin: "0 0 6px",
      fontSize: 22,
      fontWeight: 600,
      color: "var(--color-text)"
    }
  }, "Build a pipeline"), /*#__PURE__*/React.createElement("p", {
    style: {
      margin: 0,
      fontSize: 15,
      color: "var(--color-text-muted)"
    }
  }, "Describe what you want, or start from an example. Sense \u2192 Decide \u2192 Act.")), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gridTemplateColumns: "repeat(2, 1fr)",
      gap: 12
    }
  }, items.map(t => /*#__PURE__*/React.createElement("button", {
    key: t.title,
    onClick: () => onPick(t),
    style: {
      textAlign: "left",
      cursor: "pointer",
      display: "flex",
      flexDirection: "column",
      gap: 8,
      padding: 14,
      background: "var(--color-surface-elevated)",
      border: "1px solid var(--color-border)",
      borderRadius: "var(--radius-lg)",
      color: "var(--color-text)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: 8
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: t.icon,
    size: 18,
    color: "var(--color-text-secondary)"
  }), /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: 14,
      fontWeight: 600
    }
  }, t.title)), /*#__PURE__*/React.createElement("dl", {
    style: {
      margin: 0,
      display: "grid",
      gap: 3,
      fontSize: 11,
      color: "var(--color-text-muted)"
    }
  }, /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("b", {
    style: {
      color: "var(--color-badge-source)"
    }
  }, "Sense"), " \xB7 ", t.sense), /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("b", {
    style: {
      color: "var(--color-badge-transform)"
    }
  }, "Decide"), " \xB7 ", t.decide), /*#__PURE__*/React.createElement("div", null, /*#__PURE__*/React.createElement("b", {
    style: {
      color: "var(--color-badge-sink)"
    }
  }, "Act"), " \xB7 ", t.act))))));
}

/* ── Chat panel ──────────────────────────────────────────────────────────── */
function ChatPanel({
  messages,
  composing,
  empty,
  onSend,
  onPickTemplate,
  draft,
  setDraft
}) {
  const {
    ChatBubble
  } = window.ELSPETHDesignSystem_85edbb;
  const endRef = React.useRef(null);
  React.useEffect(() => {
    if (endRef.current) endRef.current.scrollTop = endRef.current.scrollHeight;
  }, [messages.length, composing]);
  function submit(e) {
    e.preventDefault();
    if (!draft.trim()) return;
    onSend(draft.trim());
  }
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      height: "100%",
      overflow: "hidden",
      background: "var(--color-surface)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "8px 16px",
      borderBottom: "1px solid var(--color-border)",
      display: "flex",
      alignItems: "center",
      gap: 12,
      flexShrink: 0
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: 13,
      fontWeight: 600,
      color: "var(--color-text)",
      flex: 1
    }
  }, "Tender evaluation"), /*#__PURE__*/React.createElement("button", {
    className: "btn-compact"
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "sparkles",
    size: 14
  }), " Switch to guided")), /*#__PURE__*/React.createElement("div", {
    ref: endRef,
    style: {
      flex: 1,
      overflowY: "auto",
      padding: "16px 0"
    }
  }, empty ? /*#__PURE__*/React.createElement(TemplateCards, {
    onPick: onPickTemplate
  }) : /*#__PURE__*/React.createElement(React.Fragment, null, messages.map((m, i) => /*#__PURE__*/React.createElement(ChatBubble, {
    key: i,
    role: m.role
  }, m.text, m.tool ? /*#__PURE__*/React.createElement(ToolCallCard, {
    tool: m.tool,
    summary: m.toolSummary,
    state: m.toolState || "committed"
  }) : null)), composing ? /*#__PURE__*/React.createElement(ComposingIndicator, null) : null)), /*#__PURE__*/React.createElement("form", {
    onSubmit: submit,
    style: {
      padding: "8px 16px",
      borderTop: "1px solid var(--color-border)",
      flexShrink: 0
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      gap: 8,
      alignItems: "flex-end"
    }
  }, /*#__PURE__*/React.createElement("button", {
    type: "button",
    className: "chat-input-icon-btn",
    "aria-label": "Attach",
    style: {
      borderRadius: "var(--radius-md)"
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "paperclip",
    size: 18
  })), /*#__PURE__*/React.createElement("textarea", {
    className: "textarea",
    "data-chat-input": true,
    rows: 1,
    value: draft,
    onChange: e => setDraft(e.target.value),
    onKeyDown: e => {
      if (e.key === "Enter" && !e.shiftKey) submit(e);
    },
    placeholder: "Describe a change, or ask the composer to build a step\u2026",
    style: {
      flex: 1,
      minHeight: 44,
      resize: "none"
    }
  }), /*#__PURE__*/React.createElement("button", {
    type: "submit",
    className: "chat-input-send-btn",
    disabled: !draft.trim(),
    "aria-label": "Send",
    style: {
      display: "inline-flex",
      alignItems: "center",
      justifyContent: "center"
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: "send",
    size: 18
  }))), /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 11,
      color: "var(--color-text-muted)",
      textAlign: "right",
      padding: "2px 0 0"
    }
  }, "Enter to send \xB7 Shift+Enter for newline")));
}

/* ── Side rail (inspection / paper) ──────────────────────────────────────── */
function SideRail({
  pipelineCount,
  validated,
  runStatus,
  running,
  onRun,
  onCopyYaml,
  onSaveReview,
  onOpenCatalog,
  onOpenGraph
}) {
  const {
    StatusBadge,
    Button
  } = window.ELSPETHDesignSystem_85edbb;
  const nodes = window.ELSPETH_KIT.pipeline;
  const checks = [{
    label: "Graph structure",
    ok: pipelineCount >= 5
  }, {
    label: "Route targets",
    ok: pipelineCount >= 5
  }, {
    label: "Edge / schema compatibility",
    ok: validated
  }, {
    label: "Secret references resolved",
    ok: validated
  }];
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      height: "100%",
      overflowY: "auto",
      background: "var(--color-surface-inspector)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "12px 12px 0"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 10,
      fontWeight: 700,
      textTransform: "uppercase",
      letterSpacing: ".08em",
      color: "var(--color-text-muted)",
      marginBottom: 8
    }
  }, "Pipeline graph"), /*#__PURE__*/React.createElement("div", {
    onClick: pipelineCount ? onOpenGraph : undefined,
    style: {
      cursor: pipelineCount ? "pointer" : "default"
    }
  }, /*#__PURE__*/React.createElement(PipelineGraph, {
    nodes: nodes,
    count: pipelineCount,
    variant: "mini"
  }))), /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "10px 12px 0"
    }
  }, validated ? /*#__PURE__*/React.createElement("div", {
    className: "validation-banner validation-banner-pass"
  }, "\u2713 Validation passed \u2014 0 errors") : pipelineCount >= 5 ? /*#__PURE__*/React.createElement("div", {
    className: "validation-banner",
    style: {
      background: "var(--color-warning-bg)",
      border: "1px solid var(--color-warning-border)",
      color: "var(--color-warning)"
    }
  }, "Ready to validate \u2014 run preflight") : /*#__PURE__*/React.createElement("div", {
    className: "validation-banner",
    style: {
      background: "var(--color-surface)",
      border: "1px solid var(--color-border)",
      color: "var(--color-text-muted)"
    }
  }, "Build a pipeline to validate")), /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "14px 12px 0"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 10,
      fontWeight: 700,
      textTransform: "uppercase",
      letterSpacing: ".08em",
      color: "var(--color-text-muted)",
      marginBottom: 8
    }
  }, "Audit readiness"), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "grid",
      gap: 6
    }
  }, checks.map(c => /*#__PURE__*/React.createElement("div", {
    key: c.label,
    style: {
      display: "flex",
      alignItems: "center",
      gap: 8,
      fontSize: 12,
      color: "var(--color-text-secondary)"
    }
  }, /*#__PURE__*/React.createElement(Icon, {
    name: c.ok ? "check-circle-2" : "circle-dashed",
    size: 15,
    color: c.ok ? "var(--color-success)" : "var(--color-text-muted)"
  }), c.label)))), /*#__PURE__*/React.createElement("div", {
    style: {
      padding: "14px 12px 0"
    }
  }, /*#__PURE__*/React.createElement("button", {
    onClick: onOpenCatalog,
    style: {
      width: "100%",
      minHeight: 44,
      padding: 8,
      display: "grid",
      gridTemplateColumns: "1fr auto",
      alignItems: "center",
      gap: 8,
      border: "1px solid var(--color-border)",
      borderRadius: "var(--radius-sm)",
      background: "var(--color-surface)",
      color: "var(--color-text)",
      cursor: "pointer",
      textAlign: "left"
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: 13,
      fontWeight: 650
    }
  }, "Plugin catalog"), /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: 10,
      fontWeight: 700,
      textTransform: "uppercase",
      color: "var(--color-text-muted)",
      border: "1px solid var(--color-border)",
      borderRadius: 4,
      padding: "2px 6px"
    }
  }, "\u2318\u21E7P"))), runStatus ? /*#__PURE__*/React.createElement("div", {
    style: {
      margin: "14px 12px 0",
      padding: 10,
      border: "1px solid var(--color-border)",
      borderRadius: "var(--radius-md)",
      background: "var(--color-surface)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      marginBottom: 6
    }
  }, /*#__PURE__*/React.createElement("span", {
    style: {
      fontSize: 11,
      fontWeight: 700,
      color: "var(--color-text-secondary)",
      textTransform: "uppercase",
      letterSpacing: ".06em"
    }
  }, "Last run"), /*#__PURE__*/React.createElement(StatusBadge, {
    status: runStatus
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 12,
      color: "var(--color-text-muted)",
      fontFamily: "var(--font-mono)"
    }
  }, "240 rows \xB7 228 approved \xB7 12 \u2192 review \xB7 0 failed")) : null, /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: "auto",
      display: "flex",
      flexDirection: "column",
      gap: 8,
      padding: 12
    }
  }, /*#__PURE__*/React.createElement(Button, {
    variant: "secondary",
    onClick: onSaveReview,
    style: {
      width: "100%"
    }
  }, "Save for review"), /*#__PURE__*/React.createElement(Button, {
    variant: "primary",
    disabled: !validated || running,
    onClick: onRun,
    style: {
      width: "100%"
    }
  }, running ? "Running…" : "Run"), /*#__PURE__*/React.createElement(Button, {
    variant: "secondary",
    onClick: onCopyYaml,
    style: {
      width: "100%"
    }
  }, "Copy YAML")));
}
Object.assign(window, {
  ComposerHeader,
  ChatPanel,
  SideRail,
  Icon
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/composer/ComposerShell.jsx", error: String((e && e.message) || e) }); }

// ui_kits/composer/LoginScreen.jsx
try { (() => {
// LoginScreen.jsx — centred local-auth card, "Sign in to ELSPETH".
function LoginScreen({
  onSignIn
}) {
  const {
    WordMark,
    Input,
    Button
  } = window.ELSPETHDesignSystem_85edbb;
  const [u, setU] = React.useState("demo");
  const [p, setP] = React.useState("demo12345");
  const [busy, setBusy] = React.useState(false);
  function submit(e) {
    e.preventDefault();
    setBusy(true);
    setTimeout(() => {
      setBusy(false);
      onSignIn();
    }, 480);
  }
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      height: "100%",
      background: "var(--color-bg)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      width: 360,
      padding: 32,
      background: "var(--color-surface)",
      borderRadius: 8,
      border: "1px solid var(--color-border)",
      boxShadow: "0 2px 8px rgba(10,40,50,0.4)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      textAlign: "center",
      marginBottom: 8
    }
  }, /*#__PURE__*/React.createElement(WordMark, {
    size: 20
  })), /*#__PURE__*/React.createElement("h1", {
    style: {
      fontSize: 18,
      fontWeight: 600,
      margin: "0 0 24px",
      textAlign: "center",
      color: "var(--color-text)"
    }
  }, "Sign in to ELSPETH"), /*#__PURE__*/React.createElement("form", {
    onSubmit: submit
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      marginBottom: 16
    }
  }, /*#__PURE__*/React.createElement(Input, {
    label: "Username",
    value: u,
    onChange: e => setU(e.target.value),
    autoComplete: "username"
  })), /*#__PURE__*/React.createElement("div", {
    style: {
      marginBottom: 24
    }
  }, /*#__PURE__*/React.createElement(Input, {
    label: "Password",
    type: "password",
    value: p,
    onChange: e => setP(e.target.value),
    autoComplete: "current-password"
  })), /*#__PURE__*/React.createElement(Button, {
    type: "submit",
    variant: "primary",
    disabled: busy,
    style: {
      width: "100%"
    }
  }, busy ? "Signing in…" : "Sign in")), /*#__PURE__*/React.createElement("p", {
    style: {
      marginTop: 16,
      fontSize: 11,
      color: "var(--color-text-muted)",
      textAlign: "center"
    }
  }, "Local development credentials. Do not reuse outside local dev.")));
}
Object.assign(window, {
  LoginScreen
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/composer/LoginScreen.jsx", error: String((e && e.message) || e) }); }

// ui_kits/composer/PipelineGraph.jsx
try { (() => {
// PipelineGraph.jsx — the Sense→Decide→Act node flow, on a faint dot-grid canvas.
const pgBadgeClass = {
  source: "type-badge-source",
  transform: "type-badge-transform",
  gate: "type-badge-gate",
  sink: "type-badge-sink",
  aggregation: "type-badge-aggregation",
  coalesce: "type-badge-coalesce"
};
const pgNodeColor = {
  source: "var(--color-badge-source)",
  transform: "var(--color-badge-transform)",
  gate: "var(--color-badge-gate)",
  sink: "var(--color-badge-sink)"
};
function PipelineNode({
  node,
  variant
}) {
  const compact = variant === "mini";
  return /*#__PURE__*/React.createElement("div", {
    style: {
      flex: "0 0 auto",
      minWidth: compact ? 78 : 150,
      padding: compact ? "6px 8px" : "10px 12px",
      background: "var(--color-surface)",
      border: "1px solid var(--color-border-strong)",
      borderLeft: `3px solid ${pgNodeColor[node.type] || "var(--color-border-strong)"}`,
      borderRadius: "var(--radius-md)"
    }
  }, compact ? /*#__PURE__*/React.createElement("span", {
    className: "type-badge " + pgBadgeClass[node.type],
    style: {
      fontSize: 9,
      padding: "1px 5px"
    }
  }, node.label) : /*#__PURE__*/React.createElement(React.Fragment, null, /*#__PURE__*/React.createElement("span", {
    className: "type-badge " + pgBadgeClass[node.type]
  }, node.label), /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 7,
      fontSize: 13,
      fontWeight: 600,
      color: "var(--color-text)"
    }
  }, node.title), /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: 2,
      fontSize: 11,
      color: "var(--color-text-muted)",
      fontFamily: "var(--font-mono)"
    }
  }, node.sub)));
}
function PipelineGraph({
  nodes,
  count = 99,
  variant = "full"
}) {
  const compact = variant === "mini";
  const shown = nodes.slice(0, count);
  const gridBg = {
    backgroundImage: "radial-gradient(var(--color-canvas-grid) 1px, transparent 1px)",
    backgroundSize: compact ? "12px 12px" : "18px 18px"
  };
  if (shown.length === 0) {
    return /*#__PURE__*/React.createElement("div", {
      style: {
        ...gridBg,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: compact ? 96 : 160,
        color: "var(--color-text-muted)",
        fontSize: 13,
        borderRadius: "var(--radius-md)"
      }
    }, "No pipeline yet");
  }
  // Split last two sinks onto a branch for the full view to show the gate fan-out.
  return /*#__PURE__*/React.createElement("div", {
    style: {
      ...gridBg,
      padding: compact ? 8 : 16,
      borderRadius: "var(--radius-md)",
      overflowX: "auto"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      alignItems: "center",
      gap: compact ? 6 : 12
    }
  }, shown.map((n, i) => /*#__PURE__*/React.createElement(React.Fragment, {
    key: n.id
  }, i > 0 ? /*#__PURE__*/React.createElement("span", {
    style: {
      flex: "0 0 auto",
      color: "var(--color-border-strong)",
      fontSize: compact ? 12 : 18
    }
  }, "\u2192") : null, /*#__PURE__*/React.createElement(PipelineNode, {
    node: n,
    variant: variant
  })))));
}
Object.assign(window, {
  PipelineGraph
});
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/composer/PipelineGraph.jsx", error: String((e && e.message) || e) }); }

// ui_kits/composer/app.jsx
try { (() => {
// app.jsx — orchestrates the ELSPETH Web Composer demo flow.
const {
  LoginScreen,
  ComposerHeader,
  ChatPanel,
  SideRail,
  CatalogDrawer,
  PipelineGraph,
  Icon
} = window;
function YamlModal({
  open,
  onClose,
  onCopy
}) {
  if (!open) return null;
  const {
    Button
  } = window.ELSPETHDesignSystem_85edbb;
  return /*#__PURE__*/React.createElement("div", {
    style: {
      position: "absolute",
      inset: 0,
      zIndex: 201,
      display: "flex",
      alignItems: "center",
      justifyContent: "center"
    }
  }, /*#__PURE__*/React.createElement("div", {
    onClick: onClose,
    style: {
      position: "absolute",
      inset: 0,
      background: "rgba(0,0,0,0.45)"
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      position: "relative",
      width: "min(560px, calc(100% - 32px))",
      maxHeight: "80%",
      background: "var(--color-surface-paper)",
      border: "1px solid var(--color-border)",
      borderRadius: 8,
      boxShadow: "0 8px 32px rgba(0,0,0,0.25)",
      display: "flex",
      flexDirection: "column"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      padding: 16,
      borderBottom: "1px solid var(--color-border)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 16,
      fontWeight: 600,
      color: "var(--color-text)"
    }
  }, "Generated YAML"), /*#__PURE__*/React.createElement("button", {
    className: "btn-compact",
    onClick: onClose,
    style: {
      minWidth: 36
    }
  }, "\xD7")), /*#__PURE__*/React.createElement("pre", {
    style: {
      flex: 1,
      overflow: "auto",
      margin: 0,
      padding: 16,
      fontFamily: "var(--font-mono)",
      fontSize: 12,
      lineHeight: 1.6,
      color: "var(--color-text-secondary)"
    }
  }, window.ELSPETH_KIT.yaml), /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      justifyContent: "flex-end",
      gap: 8,
      padding: 12,
      borderTop: "1px solid var(--color-border)"
    }
  }, /*#__PURE__*/React.createElement(Button, {
    variant: "secondary",
    onClick: onCopy
  }, "Copy"), /*#__PURE__*/React.createElement(Button, {
    variant: "primary",
    onClick: onClose
  }, "Done"))));
}
function GraphModal({
  open,
  onClose
}) {
  if (!open) return null;
  return /*#__PURE__*/React.createElement("div", {
    style: {
      position: "absolute",
      inset: 0,
      zIndex: 201,
      display: "flex",
      alignItems: "center",
      justifyContent: "center"
    }
  }, /*#__PURE__*/React.createElement("div", {
    onClick: onClose,
    style: {
      position: "absolute",
      inset: 0,
      background: "rgba(0,0,0,0.45)"
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      position: "relative",
      width: "min(840px, calc(100% - 32px))",
      background: "var(--color-surface)",
      border: "1px solid var(--color-border)",
      borderRadius: 8,
      boxShadow: "0 8px 32px rgba(0,0,0,0.25)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      padding: 16,
      borderBottom: "1px solid var(--color-border)"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      fontSize: 16,
      fontWeight: 600,
      color: "var(--color-text)"
    }
  }, "Execution graph"), /*#__PURE__*/React.createElement("button", {
    className: "btn-compact",
    onClick: onClose,
    style: {
      minWidth: 36
    }
  }, "\xD7")), /*#__PURE__*/React.createElement("div", {
    style: {
      padding: 16
    }
  }, /*#__PURE__*/React.createElement(PipelineGraph, {
    nodes: window.ELSPETH_KIT.pipeline,
    variant: "full"
  }))));
}
function Toast({
  msg
}) {
  if (!msg) return null;
  return /*#__PURE__*/React.createElement("div", {
    style: {
      position: "absolute",
      bottom: 16,
      left: "50%",
      transform: "translateX(-50%)",
      zIndex: 301,
      padding: "8px 16px",
      borderRadius: 9999,
      background: "var(--color-surface-elevated)",
      border: "1px solid var(--color-border-strong)",
      color: "var(--color-text)",
      fontSize: 13,
      boxShadow: "0 2px 8px rgba(0,0,0,0.25)"
    }
  }, msg);
}
function App() {
  const [screen, setScreen] = React.useState("login");
  const [theme, setTheme] = React.useState("dark");
  const [messages, setMessages] = React.useState([]);
  const [draft, setDraft] = React.useState("");
  const [composing, setComposing] = React.useState(false);
  const [pipelineCount, setPipelineCount] = React.useState(0);
  const [validated, setValidated] = React.useState(false);
  const [running, setRunning] = React.useState(false);
  const [runStatus, setRunStatus] = React.useState(null);
  const [catalogOpen, setCatalogOpen] = React.useState(false);
  const [yamlOpen, setYamlOpen] = React.useState(false);
  const [graphOpen, setGraphOpen] = React.useState(false);
  const [toast, setToast] = React.useState(null);
  React.useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);
  function flashToast(m) {
    setToast(m);
    setTimeout(() => setToast(null), 1800);
  }
  const push = msg => setMessages(prev => [...prev, msg]);
  function buildPipeline(userText) {
    push({
      role: "user",
      text: userText
    });
    setComposing(true);
    setTimeout(() => {
      setComposing(false);
      push({
        role: "assistant",
        text: "I built a five-node pipeline: a CSV source for the submissions, an llm transform to classify each row, and a pure-config safety gate that routes high-risk rows (risk_score > 0.8) to a review queue and the rest to an approved sink.",
        tool: "set_pipeline",
        toolSummary: "Added source · transform · gate · 2 sinks. Every edge is explicitly named.",
        toolState: "committed"
      });
      setPipelineCount(5);
      setTimeout(() => {
        setComposing(true);
        setTimeout(() => {
          setComposing(false);
          push({
            role: "system",
            text: "Preflight validation passed — graph, route targets, and edge/schema compatibility all check out."
          });
          setValidated(true);
        }, 900);
      }, 700);
    }, 1100);
  }
  function onSend(text) {
    setDraft("");
    if (pipelineCount === 0) {
      buildPipeline(text);
      return;
    }
    push({
      role: "user",
      text
    });
    setComposing(true);
    setTimeout(() => {
      setComposing(false);
      push({
        role: "assistant",
        text: "Done — that change is staged on the current pipeline version. Validate or run when you're ready.",
        tool: "update_node",
        toolSummary: "Patched the classify transform prompt.",
        toolState: "committed"
      });
    }, 1000);
  }
  function onPickTemplate(t) {
    buildPipeline("Build a " + t.title.toLowerCase() + " pipeline.");
  }
  function onRun() {
    setRunning(true);
    setRunStatus(null);
    setTimeout(() => {
      setRunning(false);
      setRunStatus("completed");
      push({
        role: "system",
        text: "Run completed — 240 rows processed. 228 approved, 12 routed to review, 0 failed. Audit trail written to Landscape."
      });
    }, 1700);
  }
  function onTryPlugin(p) {
    setCatalogOpen(false);
    onSend("Add a " + p.name + " " + p.type + " to the pipeline.");
  }
  if (screen === "login") {
    return /*#__PURE__*/React.createElement("div", {
      style: {
        height: "100%"
      }
    }, /*#__PURE__*/React.createElement(LoginScreen, {
      onSignIn: () => setScreen("composer")
    }), /*#__PURE__*/React.createElement(Toast, {
      msg: toast
    }));
  }
  return /*#__PURE__*/React.createElement("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      height: "100%",
      position: "relative"
    }
  }, /*#__PURE__*/React.createElement(ComposerHeader, {
    sessionTitle: "Tender evaluation",
    theme: theme,
    onToggleTheme: () => setTheme(theme === "dark" ? "light" : "dark"),
    onSignOut: () => {
      setScreen("login");
    }
  }), /*#__PURE__*/React.createElement("div", {
    style: {
      flex: 1,
      minHeight: 0,
      display: "grid",
      gridTemplateColumns: "1fr 320px",
      position: "relative"
    }
  }, /*#__PURE__*/React.createElement("div", {
    style: {
      overflow: "hidden",
      borderRight: "1px solid var(--color-border)"
    }
  }, /*#__PURE__*/React.createElement(ChatPanel, {
    messages: messages,
    composing: composing,
    empty: messages.length === 0,
    draft: draft,
    setDraft: setDraft,
    onSend: onSend,
    onPickTemplate: onPickTemplate
  })), /*#__PURE__*/React.createElement(SideRail, {
    pipelineCount: pipelineCount,
    validated: validated,
    running: running,
    runStatus: runStatus,
    onRun: onRun,
    onCopyYaml: () => {
      setYamlOpen(true);
    },
    onSaveReview: () => flashToast("Share link copied — reviewers must sign in"),
    onOpenCatalog: () => setCatalogOpen(true),
    onOpenGraph: () => setGraphOpen(true)
  }), /*#__PURE__*/React.createElement(CatalogDrawer, {
    open: catalogOpen,
    onClose: () => setCatalogOpen(false),
    onTry: onTryPlugin
  })), /*#__PURE__*/React.createElement(YamlModal, {
    open: yamlOpen,
    onClose: () => setYamlOpen(false),
    onCopy: () => {
      setYamlOpen(false);
      flashToast("YAML copied to clipboard");
    }
  }), /*#__PURE__*/React.createElement(GraphModal, {
    open: graphOpen,
    onClose: () => setGraphOpen(false)
  }), /*#__PURE__*/React.createElement(Toast, {
    msg: toast
  }));
}
ReactDOM.createRoot(document.getElementById("root")).render(/*#__PURE__*/React.createElement(App, null));
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/composer/app.jsx", error: String((e && e.message) || e) }); }

// ui_kits/composer/data.js
try { (() => {
// data.js — sample content for the ELSPETH Web Composer UI kit. Plain globals.
window.ELSPETH_KIT = {
  user: {
    name: "Demo User",
    id: "demo"
  },
  // Plugin catalog entries grouped by family.
  catalog: {
    sources: [{
      name: "CSV",
      kind: "csv",
      type: "source",
      description: "Read rows from a CSV file. Headers normalized to identifiers at the boundary.",
      audit: [{
        label: "strict parsing",
        tone: "positive"
      }]
    }, {
      name: "Azure Blob",
      kind: "azure_blob",
      type: "source",
      description: "Stream rows from an Azure Blob container. Malformed rows are quarantined with an audit record.",
      audit: [{
        label: "strict parsing",
        tone: "positive"
      }, {
        label: "quarantine on error",
        tone: "informational"
      }]
    }, {
      name: "Dataverse",
      kind: "dataverse",
      type: "source",
      description: "Query rows from a Microsoft Dataverse table.",
      audit: [{
        label: "typed schema",
        tone: "positive"
      }]
    }, {
      name: "JSON",
      kind: "json",
      type: "source",
      description: "Read records from a JSON or JSONL file.",
      audit: []
    }, {
      name: "Chroma",
      kind: "chroma",
      type: "source",
      description: "Retrieve documents from a Chroma vector store for RAG.",
      audit: [{
        label: "provenance tracked",
        tone: "informational"
      }]
    }],
    transforms: [{
      name: "LLM query",
      kind: "llm",
      type: "transform",
      description: "Azure OpenAI / OpenRouter query with provider pooling and multi-query.",
      audit: [{
        label: "fingerprinted secrets",
        tone: "positive"
      }, {
        label: "rate-limited",
        tone: "attention"
      }]
    }, {
      name: "Field mapper",
      kind: "field_mapper",
      type: "transform",
      description: "Rename, drop, and remap fields with contract re-typing.",
      audit: [{
        label: "contract-checked",
        tone: "positive"
      }]
    }, {
      name: "Content Safety",
      kind: "azure_content_safety",
      type: "transform",
      description: "Azure Content Safety classification at the LLM boundary.",
      audit: [{
        label: "zero-trust boundary",
        tone: "informational"
      }]
    }, {
      name: "Prompt Shield",
      kind: "prompt_shield",
      type: "transform",
      description: "Detect prompt-injection attempts on external input.",
      audit: [{
        label: "attention surfaced",
        tone: "attention"
      }]
    }, {
      name: "Threshold gate",
      kind: "gate",
      type: "gate",
      description: "Pure-config gate: route rows by a named expression.",
      audit: [{
        label: "reviewable config",
        tone: "positive"
      }]
    }, {
      name: "Batch metrics",
      kind: "batch_classifier_metrics",
      type: "aggregation",
      description: "Local, audit-attributable classifier metrics over a batch.",
      audit: [{
        label: "deterministic",
        tone: "positive"
      }]
    }],
    sinks: [{
      name: "CSV out",
      kind: "csv",
      type: "sink",
      description: "Write rows to a CSV file with restored display headers.",
      audit: [{
        label: "headers restored",
        tone: "informational"
      }]
    }, {
      name: "Review queue",
      kind: "review_queue",
      type: "sink",
      description: "Route flagged rows to a human review queue.",
      audit: [{
        label: "human-in-loop",
        tone: "informational"
      }]
    }, {
      name: "Database",
      kind: "database",
      type: "sink",
      description: "Insert rows into a relational table.",
      audit: [{
        label: "transactional",
        tone: "positive"
      }]
    }, {
      name: "Azure Blob out",
      kind: "azure_blob",
      type: "sink",
      description: "Write artifacts to an Azure Blob container.",
      audit: []
    }]
  },
  // The pipeline the assistant "builds" — revealed node-by-node.
  pipeline: [{
    id: "src",
    type: "source",
    label: "csv",
    title: "Submissions",
    sub: "source · csv"
  }, {
    id: "llm",
    type: "transform",
    label: "llm",
    title: "Classify",
    sub: "transform · llm"
  }, {
    id: "gate",
    type: "gate",
    label: "gate",
    title: "Safety gate",
    sub: "gate · risk_score > 0.8"
  }, {
    id: "ok",
    type: "sink",
    label: "csv",
    title: "Approved",
    sub: "sink · csv"
  }, {
    id: "review",
    type: "sink",
    label: "queue",
    title: "Review queue",
    sub: "sink · review_queue"
  }],
  yaml: `source:
  plugin: csv
  on_success: validated
  options:
    path: data/submissions.csv

transforms:
- name: classify
  plugin: llm
  input: validated
  on_success: classified
  options:
    prompt: "Classify the submission for abusive content."

gates:
- name: safety_gate
  input: classified
  condition: "row['risk_score'] > 0.8"
  routes:
    "true": review
    "false": approved

sinks:
  approved:
    plugin: csv
    options: { path: output/approved.csv }
  review:
    plugin: review_queue

landscape:
  url: sqlite:///./audit.db`
};
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/composer/data.js", error: String((e && e.message) || e) }); }

// ui_kits/website/site.js
try { (() => {
// site.js — shared marketing-site behaviour: render icons + theme toggle.
(function () {
  if (window.lucide) window.lucide.createIcons();
  var t = document.getElementById("theme-toggle");
  if (t) {
    t.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      t.innerHTML = '<i data-lucide="' + (next === "dark" ? "moon" : "sun") + '"></i>';
      if (window.lucide) window.lucide.createIcons();
    });
  }
})();
})(); } catch (e) { __ds_ns.__errors.push({ path: "ui_kits/website/site.js", error: String((e && e.message) || e) }); }

__ds_ns.ChatBubble = __ds_scope.ChatBubble;

__ds_ns.PluginCard = __ds_scope.PluginCard;

__ds_ns.WordMark = __ds_scope.WordMark;

__ds_ns.AlertBanner = __ds_scope.AlertBanner;

__ds_ns.Button = __ds_scope.Button;

__ds_ns.Card = __ds_scope.Card;

__ds_ns.CardHeader = __ds_scope.CardHeader;

__ds_ns.StatusBadge = __ds_scope.StatusBadge;

__ds_ns.Tabs = __ds_scope.Tabs;

__ds_ns.TypeBadge = __ds_scope.TypeBadge;

__ds_ns.Input = __ds_scope.Input;

__ds_ns.Textarea = __ds_scope.Textarea;

})();
