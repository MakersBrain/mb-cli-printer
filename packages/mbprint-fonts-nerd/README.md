# mbprint-fonts-nerd

Optional JetBrainsMono Nerd Font add-on for `mb-cli-printer`, containing
regular, bold, italic, and bold-italic faces from Nerd Fonts v3.5.1.

Install it with `uv sync --extra fonts-nerd`, or use `uv sync --extra fonts`
for every maintained font add-on. Labels should request the exact family name
`JetBrainsMono Nerd Font`.

Any other Nerd Font family is already supported through an adjacent `fonts/`
directory, `--font-dir`, `MBPRINT_FONT_DIR`, or another Python font add-on.
