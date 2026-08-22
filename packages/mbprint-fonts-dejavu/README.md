# mbprint-fonts-dejavu

Optional, redistributable font add-on for `mb-cli-printer`. It contains the
regular, bold, italic, and bold-italic faces of DejaVu Sans, DejaVu Serif, and
DejaVu Sans Mono. Installing the wheel makes the fonts available to `mbprint`
automatically through the `mbprint.font_bundles` Python entry-point group.

From the main repository:

```console
uv sync --extra fonts
```

Third-party bundles can implement the same entry point. Its loaded object may
be a directory path, an iterable of directory paths, or a zero-argument
callable returning either form.
