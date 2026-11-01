# pr-theme

> Project-R ES-DE theme — IHC-compliant badge system with Project-R color palette.

**Status:** Pre-alpha

## IHC Badge Colors

All badges follow the Project-R IHC guidelines. Red is reserved exclusively for errors.

| Badge | Hex | Meaning |
|---|---|---|
| NATIVE | `#4ecdc4` | Native recompiled binary — best experience |
| EMU | `#f5a623` | Emulation — informative, not error |
| IN PROGRESS | `#4a9eff` | Build running |
| BUILD ERROR | `#e94560` | Build failed — red = error only |

## Color Palette

| Name | Hex | Usage |
|---|---|---|
| Background | `#1a1a2e` | Panel/card backgrounds |
| Surface | `#0d0d1a` | Deep backgrounds |
| Border | `#333333` | Dividers, card borders |
| Text Primary | `#ffffff` | Headlines |
| Text Secondary | `#888888` | Descriptions |

## Structure

```
pr-theme/
  theme.xml
  badges/
    native.svg
    emu.svg
    in-progress.svg
    build-error.svg
  fonts/
    JetBrainsMono-NerdFont-Regular.ttf
```

## License

GPL-3.0
