# mbprint-fonts-phomymo

Optional font add-on matching the open web fonts offered by the
[Phomymo label designer](https://github.com/transcriptionstream/phomymo):

- Inter
- Roboto
- Open Sans
- Lato
- Montserrat
- Oswald
- Playfair Display
- Merriweather
- Roboto Mono
- Source Code Pro

Install from the main repository with `uv sync --extra fonts-phomymo`, or use
`uv sync --extra fonts` for every maintained font add-on. The wheel registers
itself through `mbprint.font_bundles`; no `--font-dir` is necessary.

Phomymo also lists operating-system fonts such as Arial, Helvetica, Georgia,
Times New Roman, Courier New, Impact, and Comic Sans MS. `mbprint` uses those
when legally installed on the host, but this wheel cannot redistribute them.

Each family directory contains its upstream SIL Open Font License notice.
