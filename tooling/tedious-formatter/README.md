# Owned Tedious diagnostic formatter

ELSPETH maintains this private package solely for the root Azurite test-tooling
tree. The root file dependency uses Tedious's `sprintf-js` lookup key; the
installed package identifies itself as `@elspeth/tedious-formatter`. The
`overrides.tedious.sprintf-js` entry references that direct file dependency,
so npm resolves it relative to the repository root. It replaces the formatter
implementation; it does not claim that upstream `sprintf-js` has been patched.
No upstream formatter source or floating-point precision implementation is
bundled. The product images do not install the root tooling tree.

The supported contract is `sprintf(format, ...values)` with `%d`, `%s`, `%X`,
`%02X`, `%04X`, `%08X` and `%%`. The whole format is validated before argument
conversion. Format length is limited to 4096 characters and padding width to
the driver's fixed hex widths. Unsupported syntax, including every floating
conversion, precision, named/positional substitution and dynamic width, raises
`TypeError`. Callers must supply their own fixed formats; this is not an API for
rendering untrusted format strings. String arguments are emitted literally.

Decimal parsing and unsigned 32-bit hex conversion preserve the behavior of
`sprintf-js` 1.1.3 used by Tedious 20.0.0. Compatibility provenance is
[sprintf-js 1.1.3](https://github.com/alexei/sprintf.js/tree/1.1.3) and the locked
Tedious diagnostic callers. The upstream BSD copyright, conditions and
disclaimer are retained in LICENSE. The replacement addresses
[CVE-2026-97058](https://github.com/advisories/GHSA-hp3w-g68c-fv3c) by eliminating
the floating-point precision feature, rather than hiding vulnerable code under
a new package name.

Run `npm ci` and `npm run test:formatter` at the repository root. Tests resolve
the formatter through the installed Azurite/Tedious consumer, inventory every
driver formatter call with Acorn, pin all twelve literal formats and check
their output, exercise real driver diagnostics, and reject dangerous precision
and width syntax without invoking the native precision methods. A driver
upgrade changing this contract must be reviewed with this package. The strict
root npm audit and Azurite blob E2E checks remain required in CI.

`npm audit` checks the remaining registry dependency tree; it does not assess
the implementation of this owned local package. Its source review, security
controls and caller compatibility checks are therefore required alongside the
audit. Baseline outputs in `compatibility.json` were measured through the
original locked formatter; its source SHA-256 was
`95add43f116385be221745307fae02d06751b01d4f939df1debb17dbe2ebf4eb`.
