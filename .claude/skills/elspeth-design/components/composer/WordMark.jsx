import React from "react";

/**
 * The ELSPETH wordmark. Renders the brand as live text in JetBrains Mono 700,
 * uppercase, 0.18em tracking — never an image. `size` sets the font size.
 */
export function WordMark({ size = 13, as: Tag = "span", className = "", style, ...rest }) {
  return (
    <Tag
      className={className}
      style={{
        fontFamily: "var(--font-mono)",
        fontWeight: 700,
        fontSize: typeof size === "number" ? `${size}px` : size,
        textTransform: "uppercase",
        letterSpacing: "0.18em",
        color: "var(--color-text)",
        ...style,
      }}
      {...rest}
    >
      ELSPETH
    </Tag>
  );
}
