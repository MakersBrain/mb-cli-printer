# Bundled fonts

Place redistributable `.ttf`, `.otf`, or `.ttc` files in this directory to
ship them inside the `mbprint` Python package. Subdirectories are scanned too.

Only include fonts whose licenses permit redistribution. `mbprint` reads the
family and style names from each font file, so filenames do not need a special
format. Include separate regular, bold, italic, and bold-italic faces when a
label requests those styles.

For a label-specific bundle, use this portable directory layout instead:

```text
my-label/
  label.json
  fonts/
    ExampleSans-Regular.ttf
    ExampleSans-Bold.ttf
```

The adjacent `fonts/` directory is discovered automatically.

The preferred application-wide bundles live under `packages/`; install all of
them with `uv sync --extra fonts`, or use `fonts-dejavu`, `fonts-phomymo`,
`fonts-nerd`, or `fonts-compatible` individually. Other font wheels can register the
`mbprint.font_bundles` entry-point group and are discovered automatically.
