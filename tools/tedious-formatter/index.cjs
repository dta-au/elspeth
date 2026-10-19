"use strict";

// BSD-3-Clause; see LICENSE and README.md for ownership and compatibility provenance.
// This is deliberately a Tedious diagnostic formatter, not a general sprintf API.
function sprintf(format, ...values) {
  if (typeof format !== "string" || format.length > 4096) {
    throw new TypeError("Tedious format must be a string of at most 4096 characters");
  }

  // Validate the entire format before coercing any arguments. Width is restricted
  // to the driver's fixed 2/4/8-character hex fields; precision is never parsed.
  const tokens = [];
  const placeholder = /%%|%(0[248])?([dsX])/y;
  let offset = 0;
  while (offset < format.length) {
    const percent = format.indexOf("%", offset);
    if (percent === -1) {
      tokens.push(format.slice(offset));
      break;
    }
    tokens.push(format.slice(offset, percent));
    placeholder.lastIndex = percent;
    const match = placeholder.exec(format);
    if (match === null || (match[1] !== undefined && match[2] !== "X")) {
      throw new TypeError("Unsupported Tedious format; expected %d, %s, %X, %02X, %04X, %08X or %%");
    }
    tokens.push(match[0] === "%%" ? "%" : { type: match[2], width: Number(match[1] ?? 0) });
    offset = placeholder.lastIndex;
  }

  let cursor = 0;
  return tokens.map((token) => {
    if (typeof token === "string") return token;
    const argument = values[cursor++];
    const value = typeof argument === "function" ? argument() : argument;
    if (token.type === "s") return String(value);
    // Match sprintf-js 1.1.3's decimal parsing and unsigned 32-bit hex behavior
    // for the driver's numeric arguments, including negative values and NaN.
    if (typeof value !== "number" && isNaN(value)) {
      throw new TypeError("Tedious numeric placeholder requires a numeric value");
    }
    const integer = parseInt(value, 10);
    if (token.type === "d") {
      return value >= 0 ? String(integer) : "-" + String(integer).replace(/^[+-]/, "");
    }
    return (integer >>> 0).toString(16).toUpperCase().padStart(token.width, "0");
  }).join("");
}

module.exports = { sprintf };
