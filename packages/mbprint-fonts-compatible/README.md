# mbprint-fonts-compatible

Optional free-font substitutes for common proprietary families:

| Requested family | Free substitute |
|---|---|
| Arial | Liberation Sans |
| Helvetica | Liberation Sans |
| Georgia | Gelasio |
| Times New Roman | Liberation Serif |
| Courier New | Liberation Mono |
| Impact | Anton |
| Comic Sans MS | Comic Neue |

Install with `uv sync --extra fonts-compatible`, or use `uv sync --extra fonts`
for every maintained bundle. Substitution happens only with the explicit
`--font-fallback` flag; strict mode continues to reject a missing requested
family.

Liberation is designed for metric compatibility with Arial, Times New Roman,
and Courier New. The other mappings are visually similar choices rather than
metric-compatible replacements.
