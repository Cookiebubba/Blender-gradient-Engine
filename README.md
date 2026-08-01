# Blender Gradient Engine

A Blender add-on for generating **seamlessly looping animated gradient backgrounds**
for websites — with a control panel in the 3D viewport, a preset library, film-grain
emulation, and WCAG contrast checking.

Built and tested against **Blender 4.5 LTS**.

## Install

1. Blender → Edit → Preferences → Add-ons → Install…
2. Pick `wave_texture_maker.py`
3. Enable **Wave Texture Maker**
4. In the 3D viewport press `N` → **Wave Tex** tab → **Set Up Scene**

## Pipelines

Three renderers share one colour, effects and export stack. The panel only shows
the controls belonging to the active pipeline.

**Aura Flow** — domain-warped fBm. Noise bends its own sample coordinates twice
before the colour lookup, producing large soft organic blobs. This is the one to
use for modern mesh-gradient backgrounds; a wave texture can only make bands.

**Wave Gradient** — the original procedural wave. Directional bands, water or
ripple profile.

**Iridescent Film** — actual physics. A spectral gravity-capillary wave solver
(Tessendorf-style, `wavetex_physics.py`) drives film thickness, and colour comes
from the Airy reflectance of a thin film integrated against the CIE 1931 colour
matching functions. The rainbow is computed, not painted.

## Looping

Everything loops exactly, by construction rather than by crossfade.

- Aura: each warp layer samples along a circular path, so one phase cycle returns
  to its starting coordinate. Layers orbit at different radii, so the field morphs
  instead of sliding.
- Iridescent: every Fourier mode is snapped to an integer multiple of the loop
  frequency, so each completes a whole number of cycles over the loop.

Measured closure is within one 8-bit step.

## Film grain

Grain is modelled rather than pasted on:

- spatial correlation is imposed in the frequency domain, which also makes the
  tile exactly periodic so it wraps with no seam
- the blue-sensitive dye layer is the coarsest, as in real colour negative
- amplitude follows a density curve peaking in the midtones — grain is added in
  linear light, and the display transform expands shadows steeply, so a flat
  amplitude gets hammered in the darks
- enlargement is a separate control: a smaller negative is blown up more to reach
  the same frame, which is *why* 16mm looks grainier than 35mm

Stocks: 35mm Fine / Standard / Pushed, 16mm, Super 8, B&W 400.

## Dithering

Six pattern types, each with cell size, screen angle and per-channel colour
separation — the controls that decide whether a gradient reads as printed
matter or as a computer artefact.

| Mode | Character |
|---|---|
| Blue Noise | low frequencies removed, so it reads as texture not dirt — the default |
| White Noise | plain random, grittier |
| Ordered (Bayer) | crosshatch grid, selectable matrix order |
| Halftone Dots | clustered dots growing from each cell, like print |
| Line Screen | parallel lines — engraving, risograph |
| Crosshatch | two crossed line screens |

Two details that matter:

- **Cell size** decides the whole register. At 1px it is invisible
  anti-banding; at 4–12px the pattern becomes the look.
- **Colour separation** gives each channel its own screen angle (15°/75°/0°,
  as in four-colour print). This is what stops the dots stacking into a moire
  and gives halftones their colour life.

Periodic screens are clamped to a 2px minimum cell — a cosine screen with a
one-pixel period samples the same phase at every pixel and collapses to a flat
field.

## Design tools

- **Palette** from a seed with colour-harmony rules, or from your own brand
  colours blended through OKLab (linear RGB drags mid-stops toward grey)
- **Colour zones** — quantise the field into flat territories that take palette
  colours verbatim, then soften the edges. Interpolating first and posterising
  after produces muddy mid-hues
- **Balance Zones** — measures the field's histogram and flattens it so every
  palette colour actually gets area. Blender's noise clusters around 0.5, so
  uniform quantiser buckets silently starve the outer zones
- **Contrast check** — samples the render and reports worst-case WCAG ratio
  against your text colour
- **Scrim, tone range, edge fade** for keeping a background under readable copy
- **Web export** with a poster frame and a handoff snippet

## Repository layout

| Path | What it is |
|---|---|
| `wave_texture_maker.py` | the add-on |
| `wavetex_physics.py` | wave solver and thin-film reflectance, standalone/testable |
| `wave_texture_maker.blend` | working scene |

Rendered frames and baked simulation caches are gitignored — the add-on
regenerates them.
