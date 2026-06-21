# Console / platform selector icons

Monochrome single-path brand glyphs used by the opponent-platform filter in
the left sidebar (`_filter_sidebar` in `server.py`). They are rendered inline
with `fill="currentColor"` so they inherit the chip text color and tint to the
accent on hover/active, matching the rest of the selector.

| File              | Platform        | Source                                  |
|-------------------|-----------------|-----------------------------------------|
| `steam.svg`       | Steam           | simple-icons (`steam`)                  |
| `epic.svg`        | Epic            | simple-icons (`epicgames`)              |
| `playstation.svg` | PlayStation     | simple-icons (`playstation`)            |
| `xbox.svg`        | Xbox            | simple-icons (`xbox`)                   |
| `switch.svg`      | Nintendo Switch | simple-icons (`nintendoswitch`)         |

All five platforms exist on selfh.st Icons (https://selfh.st/icons/), but
selfh.st ships them as full-color marks. The selector is monochrome and
theme-tinted, so the simple-icons single-color equivalents were vendored
instead to match the existing style. simple-icons is licensed CC0-1.0.
