# Fonts

| File | Source | License |
| --- | --- | --- |
| `Inter-Medium.ttf`, `Inter-SemiBold.ttf` | [rsms/inter](https://github.com/rsms/inter) 4.x, Latin subset via [Fontsource](https://fontsource.org/fonts/inter) | SIL Open Font License 1.1, see `OFL-Inter.txt` |
| `Inter-SemiBold-latin-ext.ttf` | Same, Latin Extended subset (Turkish İ, ş, ğ) via Fontsource | SIL Open Font License 1.1, see `OFL-Inter.txt` |
| `Inter-SemiBold-tnum.ttf` | `Inter-SemiBold.ttf` with the `tnum` feature frozen into the cmap (`pyftfeatfreeze -f tnum`), so digits share one width | SIL Open Font License 1.1, see `OFL-Inter.txt` |
| `MaterialIcons-Regular.ttf` | [google/material-design-icons](https://github.com/google/material-design-icons) | Apache License 2.0, see `LICENSE-MaterialIcons.txt` |

All are embedded in the firmware unmodified and rendered at runtime by LVGL's Tiny TTF.
Inter is used without kerning: stb_truetype's per-pair GPOS lookups made redraws far slower.
