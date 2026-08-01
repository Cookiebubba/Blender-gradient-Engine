bl_info = {
    "name": "Wave Texture Maker",
    "author": "Brandyn",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Wave Tex",
    "description": "Animated looping wave/ripple gradient texture generator with seed palettes, "
                   "color harmony, noise and big overlays, color filters and normal control.",
    "category": "Material",
}

import bpy
import math
import os
import glob
import json
import random
import colorsys
import numpy as np

MAT_NAME = "WaveGradientMat"
PLANE_NAME = "TexturePlane"
TAU = 6.283185307


# ---------------------------------------------------------------- palette ---

HARMONY_OFFSETS = {
    'ANALOGOUS':     [-0.10, -0.05, 0.0, 0.05, 0.10],
    'COMPLEMENTARY': [0.0, 0.03, 0.5, 0.53, 0.97],
    'TRIADIC':       [0.0, 0.333, 0.666, 0.02, 0.35],
    'TETRADIC':      [0.0, 0.25, 0.5, 0.75, 0.02],
    'SPLIT':         [0.0, 0.417, 0.583, 0.03, 0.45],
    'MONOCHROME':    [0.0, 0.0, 0.0, 0.0, 0.0],
}


def gen_palette(seed, harmony, sat, bright, stops):
    """Deterministic palette from a seed, laid out along a harmony wheel.

    Hues are sorted before use: adjacent ramp stops that jump far around the
    wheel interpolate through grey, which kills the gradient.
    """
    rng = random.Random(seed)
    base = rng.random()
    offs = HARMONY_OFFSETS.get(harmony, HARMONY_OFFSETS['ANALOGOUS'])
    hues = [offs[i % len(offs)] for i in range(stops)]
    hues.sort()
    if rng.random() < 0.5:
        hues.reverse()
    hues = [(base + h) % 1.0 for h in hues]

    cols = []
    for i, h in enumerate(hues):
        t = i / max(1, stops - 1)
        v = 0.35 + 0.65 * t
        s = 1.0 - 0.35 * t
        if harmony == 'MONOCHROME':
            s = 1.0 - 0.75 * t
        v = max(0.0, min(1.0, v * bright))
        s = max(0.0, min(1.0, s * sat))
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        cols.append((r, g, b, 1.0))
    return cols


# ---- OKLab: perceptual blending. Interpolating brand colours in linear RGB
# ---- drags mid-stops toward grey; OKLab keeps them saturated and on-hue.

def _srgb_to_oklab(c):
    r, g, b = c[0], c[1], c[2]
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _oklab_to_srgb(lab):
    L, a, b = lab
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (max(0.0, 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
            max(0.0, -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
            max(0.0, -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s))


def brand_palette(colors, stops, lift=0.0):
    """Blend the brand colours through OKLab into `stops` ramp entries."""
    labs = [_srgb_to_oklab(c) for c in colors]
    if len(labs) == 1:
        labs = labs * 2
    out = []
    segs = len(labs) - 1
    for i in range(stops):
        t = i / max(1, stops - 1)
        pos = t * segs
        j = min(int(pos), segs - 1)
        f = pos - j
        lab = tuple(labs[j][k] * (1 - f) + labs[j + 1][k] * f for k in range(3))
        if lift:
            lab = (min(1.0, max(0.0, lab[0] + lift * (t - 0.5) * 2.0)), lab[1], lab[2])
        out.append(tuple(_oklab_to_srgb(lab)) + (1.0,))
    return out


def nodes():
    mat = bpy.data.materials.get(MAT_NAME)
    return mat.node_tree.nodes if mat else None


def apply_palette(colors):
    """Push a palette into every pipeline's ramp, so switching pipelines keeps
    the colours you picked."""
    for mat_name in (MAT_NAME, AURA_MAT):
        mat = bpy.data.materials.get(mat_name)
        if not mat or 'GradientRamp' not in mat.node_tree.nodes:
            continue
        ramp = mat.node_tree.nodes['GradientRamp'].color_ramp
        while len(ramp.elements) > 1:
            ramp.elements.remove(ramp.elements[-1])
        ramp.elements[0].position = 0.0
        ramp.elements[0].color = colors[0]
        for i, c in enumerate(colors[1:], start=1):
            ramp.elements.new(i / (len(colors) - 1)).color = c


# ------------------------------------------------------------ material ------

def build_material():
    mat = bpy.data.materials.get(MAT_NAME) or bpy.data.materials.new(MAT_NAME)
    # Fake user, or Blender drops this material on save whenever another
    # pipeline is the one assigned to the plane - zero users means purged.
    mat.use_fake_user = True
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    n = nt.nodes
    L = nt.links.new

    def nd(kind, name, x, y):
        node = n.new(kind)
        node.name = node.label = name
        node.location = (x, y)
        return node

    # phase drives every animated element, so one loop == one 2*pi sweep
    phase = nd('ShaderNodeValue', 'PhaseValue', -1900, 300)
    speed = nd('ShaderNodeMath', 'SpeedMul', -1750, 300)
    speed.operation = 'MULTIPLY'
    speed.inputs[1].default_value = 1.0
    L(phase.outputs[0], speed.inputs[0])

    texco = nd('ShaderNodeTexCoord', 'TexCo', -1900, -100)
    center = nd('ShaderNodeVectorMath', 'CenterCoord', -1750, -100)
    center.operation = 'SUBTRACT'
    center.inputs[1].default_value = (0.5, 0.5, 0.0)
    L(texco.outputs['Generated'], center.inputs[0])
    base = nd('ShaderNodeMapping', 'BaseMap', -1600, -100)
    L(center.outputs[0], base.inputs['Vector'])

    wave = nd('ShaderNodeTexWave', 'WaveTex', -1000, 200)
    wave.wave_type = 'BANDS'
    wave.bands_direction = 'DIAGONAL'
    wave.wave_profile = 'SIN'
    wave.inputs['Scale'].default_value = 4.0
    wave.inputs['Distortion'].default_value = 6.0
    wave.inputs['Detail'].default_value = 2.0
    L(base.outputs['Vector'], wave.inputs['Vector'])
    L(speed.outputs[0], wave.inputs['Phase Offset'])

    # noise samples a circular orbit so it returns exactly to its start
    pcos = nd('ShaderNodeMath', 'PhaseCos', -1400, -300); pcos.operation = 'COSINE'
    psin = nd('ShaderNodeMath', 'PhaseSin', -1400, -470); psin.operation = 'SINE'
    L(phase.outputs[0], pcos.inputs[0])
    L(phase.outputs[0], psin.inputs[0])
    cm = nd('ShaderNodeMath', 'CosMul', -1250, -300); cm.operation = 'MULTIPLY'; cm.inputs[1].default_value = 0.5
    sm = nd('ShaderNodeMath', 'SinMul', -1250, -470); sm.operation = 'MULTIPLY'; sm.inputs[1].default_value = 0.5
    L(pcos.outputs[0], cm.inputs[0])
    L(psin.outputs[0], sm.inputs[0])
    off = nd('ShaderNodeCombineXYZ', 'NoiseOffsetVec', -1100, -380)
    L(cm.outputs[0], off.inputs['X'])
    L(sm.outputs[0], off.inputs['Y'])
    ncoord = nd('ShaderNodeVectorMath', 'NoiseCoord', -950, -280); ncoord.operation = 'ADD'
    L(base.outputs['Vector'], ncoord.inputs[0])
    L(off.outputs[0], ncoord.inputs[1])
    noise = nd('ShaderNodeTexNoise', 'NoiseTex', -800, -280)
    noise.inputs['Scale'].default_value = 6.0
    noise.inputs['Detail'].default_value = 4.0
    L(ncoord.outputs[0], noise.inputs['Vector'])

    facmix = nd('ShaderNodeMix', 'FacMix', -600, 200)
    facmix.data_type = 'FLOAT'
    facmix.inputs['Factor'].default_value = 0.15
    L(wave.outputs['Fac'], facmix.inputs['A'])
    L(noise.outputs['Fac'], facmix.inputs['B'])

    gam = nd('ShaderNodeMath', 'FacGamma', -440, 320); gam.operation = 'POWER'
    gam.inputs[1].default_value = 1.0
    L(facmix.outputs['Result'], gam.inputs[0])
    cyc = nd('ShaderNodeMath', 'FacCycles', -440, 150); cyc.operation = 'MULTIPLY'
    cyc.inputs[1].default_value = 1.0
    L(gam.outputs[0], cyc.inputs[0])
    ping = nd('ShaderNodeMath', 'FacPingPong', -440, -20); ping.operation = 'PINGPONG'
    ping.inputs[1].default_value = 1.0
    L(cyc.outputs[0], ping.inputs[0])

    ramp = nd('ShaderNodeValToRGB', 'GradientRamp', -260, 200)
    ramp.color_ramp.interpolation = 'EASE'
    L(ping.outputs[0], ramp.inputs['Fac'])

    # big overlay rotates with phase, so it also lands back on frame 0
    orot = nd('ShaderNodeCombineXYZ', 'OverlayRotVec', -1250, -700)
    L(phase.outputs[0], orot.inputs['Z'])
    omap = nd('ShaderNodeMapping', 'OverlayMap', -1100, -650)
    L(base.outputs['Vector'], omap.inputs['Vector'])
    L(orot.outputs[0], omap.inputs['Rotation'])
    magic = nd('ShaderNodeTexMagic', 'BigOverlayMagic', -900, -600)
    magic.inputs['Scale'].default_value = 1.5
    L(omap.outputs[0], magic.inputs['Vector'])
    voro = nd('ShaderNodeTexVoronoi', 'BigOverlayVoronoi', -900, -820)
    voro.inputs['Scale'].default_value = 2.0
    L(omap.outputs[0], voro.inputs['Vector'])

    ovmix = nd('ShaderNodeMix', 'OverlayMix', -40, 200)
    ovmix.data_type = 'RGBA'
    ovmix.blend_type = 'OVERLAY'
    ovmix.inputs['Factor'].default_value = 0.0
    L(ramp.outputs['Color'], ovmix.inputs['A'])

    hs = nd('ShaderNodeHueSaturation', 'FilterHueSat', 140, 250)
    hs.inputs['Hue'].default_value = 0.5
    L(ovmix.outputs['Result'], hs.inputs['Color'])
    bc = nd('ShaderNodeBrightContrast', 'FilterContrast', 310, 250)
    L(hs.outputs['Color'], bc.inputs['Color'])
    tint = nd('ShaderNodeMix', 'FilterTint', 480, 250)
    tint.data_type = 'RGBA'
    tint.blend_type = 'COLOR'
    tint.inputs['Factor'].default_value = 0.0
    tint.inputs[7].default_value = (1.0, 0.4, 0.8, 1.0)
    L(bc.outputs['Color'], tint.inputs['A'])

    bump = nd('ShaderNodeBump', 'BumpNode', 480, -200)
    bump.inputs['Strength'].default_value = 0.3
    L(facmix.outputs['Result'], bump.inputs['Height'])

    emi = nd('ShaderNodeEmission', 'FlatEmission', 720, 400)
    L(tint.outputs['Result'], emi.inputs['Color'])
    pb = nd('ShaderNodeBsdfPrincipled', 'PBSDF', 720, 100)
    pb.inputs['Roughness'].default_value = 0.4
    pb.inputs['Emission Strength'].default_value = 1.0
    L(tint.outputs['Result'], pb.inputs['Base Color'])
    L(tint.outputs['Result'], pb.inputs['Emission Color'])
    L(bump.outputs['Normal'], pb.inputs['Normal'])

    mix = nd('ShaderNodeMixShader', 'ShadeMix', 960, 250)
    mix.inputs['Fac'].default_value = 0.0
    L(emi.outputs['Emission'], mix.inputs[1])
    L(pb.outputs['BSDF'], mix.inputs[2])
    out = nd('ShaderNodeOutputMaterial', 'MatOut', 1160, 250)
    L(mix.outputs['Shader'], out.inputs['Surface'])

    try:
        phase.outputs[0].driver_remove('default_value')
    except Exception:
        pass
    drv = phase.outputs[0].driver_add('default_value').driver
    drv.type = 'SCRIPTED'
    drv.expression = "frame / 120 * %s" % TAU
    return mat


def build_stage(ctx):
    scene = ctx.scene
    plane = bpy.data.objects.get(PLANE_NAME)
    if plane is None:
        bpy.ops.mesh.primitive_plane_add(size=2, location=(0, 0, 0))
        plane = ctx.active_object
        plane.name = PLANE_NAME

    cam = bpy.data.objects.get("Camera")
    if cam is None or cam.type != 'CAMERA':
        cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
        scene.collection.objects.link(cam)
    # The plane is square but the frame usually is not, so the texture spills
    # past the camera bounds. Blanking everything outside the frame makes the
    # camera view show exactly what renders and nothing else.
    cam.data.show_passepartout = True
    cam.data.passepartout_alpha = 1.0
    cam.data.type = 'ORTHO'
    cam.data.ortho_scale = 2.0
    cam.location = (0, 0, 3)
    cam.rotation_euler = (0, 0, 0)
    scene.camera = cam

    if scene.render.resolution_x <= 0:
        scene.render.resolution_x = 1024
        scene.render.resolution_y = 1024
    # the dither patterns are pixel-exact with the output, so anything other
    # than 100% would resample them and destroy the dither
    scene.render.resolution_percentage = 100
    # 30fps is what every production background asset measured actually ships
    # (Apple, Arc, Framer, Claude, Resend); Blender's 24 is a film default.
    if scene.render.fps == 24 and scene.render.fps_base == 1.0:
        scene.render.fps = 30
    scene.frame_start = 1

    mat = build_material()
    build_aura_material()      # cheap, node-only; keeps pipeline switching instant
    plane.data.materials.clear()
    plane.data.materials.append(mat)
    return plane


# ------------------------------------------- post effects / compositor ------

def _bayer(n=8):
    m = np.array([[0, 2], [3, 1]], dtype=np.float64)
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return m / m.size


def _pattern_image(name, arr):
    """Store a signed (-0.5..0.5) pattern in a float, non-color image.

    `arr` may be (h, w) for a mono pattern or (h, w, 3) for one that carries a
    different value per channel.
    """
    h, w = arr.shape[0], arr.shape[1]
    img = bpy.data.images.get(name)
    if img and (img.size[0] != w or img.size[1] != h):
        bpy.data.images.remove(img)
        img = None
    if img is None:
        img = bpy.data.images.new(name, width=w, height=h, alpha=False, float_buffer=True)
    img.colorspace_settings.name = 'Non-Color'
    # Fake user, or Blender purges the tile the moment a mode stops referencing
    # it - switching dither mode was quietly deleting the other pattern.
    img.use_fake_user = True
    rgba = np.empty((h, w, 4), dtype=np.float32)
    if arr.ndim == 3:
        rgba[:, :, :3] = arr
    else:
        rgba[:, :, 0] = rgba[:, :, 1] = rgba[:, :, 2] = arr
    rgba[:, :, 3] = 1.0
    img.pixels.foreach_set(rgba.ravel())
    img.update()
    return img


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


# ---------------------------------------------------------------- grain -----
#
# Film grain is clumps of developed silver halide, not per-pixel static. Three
# properties follow from that and none of them come out of a plain random
# array:
#   * crystals have a physical SIZE, so neighbouring pixels are correlated
#   * a smaller negative is enlarged more to reach the same frame, which is why
#     16mm looks grainier than 35mm at identical stock - that is the SCALE
#   * the three dye layers grow independently, so colour negative grain is
#     chromatic; black and white stock is luminance-only
#
# Correlation is imposed in the frequency domain, which also makes the result
# exactly periodic - so the tile wraps seamlessly with no visible seam.

def _clumped_noise(rng, h, w, sigma):
    n = rng.normal(0.0, 1.0, (h, w))
    if sigma <= 0.01:
        return n
    ky = np.fft.fftfreq(h)[:, None]
    kx = np.fft.fftfreq(w)[None, :]
    transfer = np.exp(-2.0 * (np.pi ** 2) * (sigma ** 2) * (kx ** 2 + ky ** 2))
    out = np.real(np.fft.ifft2(np.fft.fft2(n) * transfer))
    s = out.std()
    return out / s if s > 1e-9 else out


def build_grain(w, h, size=0.85, roughness=0.5, scale=1.15, chroma=0.30, seed=1):
    """One tile of film grain, signed and normalised to +/-0.5."""
    # Enlargement is folded into the correlation radius and the tile is built
    # straight at output size. Generating small and pixel-doubling up to the
    # frame duplicates whole rows and columns on a regular pitch, which stripes
    # the noise - visible as structured artifacting rather than grain.
    sigma = max(0.0, float(size)) * max(0.05, float(scale))
    rng = np.random.default_rng(seed)
    layers = []
    for c in range(3):
        # the blue-sensitive layer of colour negative is the coarsest
        layers.append(_clumped_noise(rng, h, w, sigma * (1.0 + 0.18 * c)))
    n = np.stack(layers, axis=-1)

    # Roughness sharpens the clump edges. Below 0.5 the grain stays creamy,
    # above it the crystals read as distinct specks.
    if abs(roughness - 0.5) > 1e-3:
        k = 0.35 + 1.6 * float(roughness)
        n = np.sign(n) * (np.abs(n) ** (1.0 / max(0.2, k)))
        s = n.std()
        if s > 1e-9:
            n /= s

    # Film grain is mostly a LUMINANCE fluctuation with a smaller chroma
    # component. Fully independent channels give every pixel a random saturated
    # hue, which reads as rainbow sensor static, not film.
    mono = n.mean(axis=2, keepdims=True)
    n = mono * (1.0 - chroma) + n * chroma
    s = n.std()
    if s > 1e-9:
        n /= s                      # keep amplitude constant across chroma

    return (n * 0.30).clip(-0.5, 0.5).astype(np.float32)


def build_scrim(w, h, direction, coverage, softness):
    """Alpha ramp used to guarantee text legibility over the background."""
    yy, xx = np.mgrid[0:h, 0:w]
    u, v = xx / max(1, w - 1), yy / max(1, h - 1)
    v = 1.0 - v                                  # image rows run bottom-up
    soft = max(1e-3, softness)
    if direction == 'BOTTOM':
        t = (coverage - v) / soft
    elif direction == 'TOP':
        t = (v - (1.0 - coverage)) / soft
    elif direction == 'LEFT':
        t = (coverage - u) / soft
    elif direction == 'RIGHT':
        t = (u - (1.0 - coverage)) / soft
    elif direction == 'RADIAL':
        r = np.hypot(u - 0.5, v - 0.5) / 0.7071
        t = (r - (1.0 - coverage)) / soft
    else:                                        # FULL - flat wash
        t = np.ones_like(u)
    return _smoothstep(t).astype(np.float32)


def build_edge_fade(w, h, inset, softness):
    yy, xx = np.mgrid[0:h, 0:w]
    u, v = xx / max(1, w - 1), yy / max(1, h - 1)
    d = np.minimum(np.minimum(u, 1.0 - u), np.minimum(v, 1.0 - v))
    return _smoothstep((d - inset) / max(1e-3, softness)).astype(np.float32)


def build_patterns(w, h, seed=1, scrim=None, edge=None, grain=None):
    """Dither/grain patterns must be pixel-exact with the render, so they are
    rebuilt whenever the output resolution changes."""
    b = _bayer(8)
    tiled = np.tile(b, (h // 8 + 1, w // 8 + 1))[:h, :w]
    _pattern_image("WT_DitherBayer", (tiled - 0.5).astype(np.float32))
    rng = np.random.default_rng(seed)
    _pattern_image("WT_DitherNoise", (rng.random((h, w)) - 0.5).astype(np.float32))
    gk = grain or {}
    _pattern_image("WT_Grain", build_grain(w, h, seed=seed, **gk))
    if scrim is not None:
        _pattern_image("WT_Scrim", build_scrim(w, h, *scrim))
    if edge is not None:
        _pattern_image("WT_EdgeFade", build_edge_fade(w, h, *edge))


def build_compositor(scene):
    scene.use_nodes = True
    t = scene.node_tree
    t.nodes.clear()
    N = t.nodes
    L = t.links.new

    def nd(kind, name, x, y):
        n = N.new(kind)
        n.name = n.label = name
        n.location = (x, y)
        return n

    rl = nd('CompositorNodeRLayers', 'RenderIn', -1500, 0)

    lens = nd('CompositorNodeLensdist', 'FX_Lens', -1280, 0)
    lens.use_fit = True
    lens.inputs['Distortion'].default_value = 0.0
    lens.inputs['Dispersion'].default_value = 0.0
    L(rl.outputs['Image'], lens.inputs['Image'])

    # 4.5 takes the blur radius from the Size vector input (in pixels); the old
    # size_x / factor_x properties are ignored.
    blur = nd('CompositorNodeBlur', 'FX_Blur', -1080, 0)
    blur.filter_type = 'FAST_GAUSS'
    blur.inputs['Size'].default_value = (0.0, 0.0)
    blur.inputs['Extend Bounds'].default_value = True
    L(lens.outputs['Image'], blur.inputs['Image'])

    kuw = nd('CompositorNodeKuwahara', 'FX_Painterly', -880, 0)
    kuw.variation = 'ANISOTROPIC'
    kuw.inputs['Size'].default_value = 6.0
    kuw.mute = True
    L(blur.outputs['Image'], kuw.inputs['Image'])

    pix = nd('CompositorNodePixelate', 'FX_Pixelate', -690, 0)
    pix.inputs['Size'].default_value = 1
    pix.mute = True
    L(kuw.outputs['Image'], pix.inputs['Color'])

    glare = nd('CompositorNodeGlare', 'FX_Bloom', -500, 0)
    glare.glare_type = 'BLOOM'
    glare.quality = 'MEDIUM'
    glare.inputs['Strength'].default_value = 0.0
    glare.inputs['Threshold'].default_value = 0.8
    glare.inputs['Size'].default_value = 7.0
    L(pix.outputs['Color'], glare.inputs['Image'])

    # dither: add a signed pattern, then quantise. Order matters - the pattern
    # has to be added *before* the posterize or it does nothing.
    dimg = nd('CompositorNodeImage', 'DitherPattern', -500, -420)
    dimg.image = bpy.data.images.get("WT_DitherBayer")
    dadd = nd('CompositorNodeMixRGB', 'DitherAdd', -280, 0)
    dadd.blend_type = 'ADD'
    dadd.inputs['Fac'].default_value = 0.0
    L(glare.outputs['Image'], dadd.inputs[1])
    L(dimg.outputs['Image'], dadd.inputs[2])

    post = nd('CompositorNodePosterize', 'FX_Posterize', -90, 0)
    post.inputs['Steps'].default_value = 8
    post.mute = True
    L(dadd.outputs['Image'], post.inputs['Image'])

    gimg = nd('CompositorNodeImage', 'GrainImage', -280, -640)
    gimg.image = bpy.data.images.get("WT_Grain")
    gtr = nd('CompositorNodeTranslate', 'GrainTranslate', -90, -640)
    gtr.wrap_axis = 'BOTH'          # wrapping is what lets the grain loop
    L(gimg.outputs['Image'], gtr.inputs['Image'])

    # Real film/scan grain is strongest in the mid and low tones and squeezes
    # toward white - measured correlation between tile brightness and grain
    # sigma on grainient plates is -0.83. Uniform full-frame grain is the tell
    # of an "add noise" checkbox, so modulate amplitude by inverse luminance.
    glum = nd('CompositorNodeRGBToBW', 'GrainLum', -90, -420)
    L(post.outputs['Image'], glum.inputs['Image'])
    # Density curve. Grain is added in LINEAR light, but the display transform
    # expands dark values steeply - so a perturbation that is modest in linear
    # terms explodes visually in the shadows. Real film peaks in the midtones
    # and falls away at BOTH ends, which is what this ramp encodes. A plain
    # highlight rolloff leaves the shadows getting hammered.
    gamp = nd('CompositorNodeValToRGB', 'GrainAmp', 90, -420)
    _ramp_set(gamp, [(0.00, (0.10, 0.10, 0.10, 1)),
                     (0.10, (0.45, 0.45, 0.45, 1)),
                     (0.35, (1.00, 1.00, 1.00, 1)),
                     (0.65, (0.90, 0.90, 0.90, 1)),
                     (1.00, (0.22, 0.22, 0.22, 1))], 'LINEAR')
    L(glum.outputs['Val'], gamp.inputs['Fac'])
    gmod = nd('CompositorNodeMixRGB', 'GrainMod', 280, -640)
    gmod.blend_type = 'MULTIPLY'
    gmod.inputs['Fac'].default_value = 1.0
    # Operand order matters even though MULTIPLY is commutative: a mix takes
    # its canvas from input A. Feeding the TRANSLATED grain into A gives the
    # result that tile's shifted domain, so the strip the shift uncovered gets
    # no grain at all - a hard edge partway across the frame. Taking the canvas
    # from the full-frame amplitude map instead keeps coverage whole.
    L(gamp.outputs['Image'], gmod.inputs[1])
    L(gtr.outputs['Image'], gmod.inputs[2])

    gadd = nd('CompositorNodeMixRGB', 'GrainAdd', 470, 0)
    gadd.blend_type = 'ADD'
    gadd.inputs['Fac'].default_value = 0.0
    L(post.outputs['Image'], gadd.inputs[1])
    L(gmod.outputs['Image'], gadd.inputs[2])

    ell = nd('CompositorNodeEllipseMask', 'VignetteMask', -280, -900)
    ell.inputs['Size'].default_value = (0.85, 0.85)
    vbl = nd('CompositorNodeBlur', 'VignetteBlur', -90, -900)
    vbl.filter_type = 'FAST_GAUSS'
    vbl.inputs['Size'].default_value = (120.0, 120.0)
    L(ell.outputs['Mask'], vbl.inputs['Image'])
    vmul = nd('CompositorNodeMixRGB', 'VignetteMul', 330, 0)
    vmul.blend_type = 'MULTIPLY'
    vmul.inputs['Fac'].default_value = 0.0
    L(gadd.outputs['Image'], vmul.inputs[1])
    L(vbl.outputs['Image'], vmul.inputs[2])

    # Tone range: squeeze the image into a luminance band so it cannot compete
    # with text. Done as scale+lift with colour operands - MapRange's Value
    # socket is scalar and would flatten the image to greyscale.
    tscale = nd('CompositorNodeMixRGB', 'ToneScale', 440, 0)
    tscale.blend_type = 'MULTIPLY'
    tscale.inputs['Fac'].default_value = 1.0
    tscale.inputs[2].default_value = (1.0, 1.0, 1.0, 1.0)
    L(vmul.outputs['Image'], tscale.inputs[1])
    tone = nd('CompositorNodeMixRGB', 'FX_ToneRange', 600, 0)
    tone.blend_type = 'ADD'
    tone.inputs['Fac'].default_value = 1.0
    tone.inputs[2].default_value = (0.0, 0.0, 0.0, 1.0)
    L(tscale.outputs['Image'], tone.inputs[1])

    # Scrim: a soft wash of flat colour where copy will sit
    simg = nd('CompositorNodeImage', 'ScrimImage', 520, -420)
    simg.image = bpy.data.images.get("WT_Scrim")
    sstr = nd('CompositorNodeMath', 'ScrimStrength', 700, -420)
    sstr.operation = 'MULTIPLY'
    sstr.inputs[1].default_value = 0.0
    sstr.use_clamp = True
    L(simg.outputs['Image'], sstr.inputs[0])
    scol = nd('CompositorNodeRGB', 'ScrimColor', 700, -620)
    scol.outputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
    smix = nd('CompositorNodeMixRGB', 'ScrimMix', 900, 0)
    smix.blend_type = 'MIX'
    L(sstr.outputs[0], smix.inputs[0])
    L(tone.outputs['Image'], smix.inputs[1])
    L(scol.outputs[0], smix.inputs[2])

    expo = nd('CompositorNodeExposure', 'FX_Exposure', 1090, 0)
    expo.inputs['Exposure'].default_value = 0.0
    L(smix.outputs['Image'], expo.inputs['Image'])

    # Edge fade drives alpha so the plate can be layered over a page
    eimg = nd('CompositorNodeImage', 'EdgeImage', 1090, -420)
    eimg.image = bpy.data.images.get("WT_EdgeFade")
    ealpha = nd('CompositorNodeSetAlpha', 'FX_EdgeAlpha', 1280, 0)
    ealpha.mode = 'REPLACE_ALPHA'
    ealpha.mute = True
    L(expo.outputs['Image'], ealpha.inputs['Image'])
    L(eimg.outputs['Image'], ealpha.inputs['Alpha'])

    comp = nd('CompositorNodeComposite', 'CompOut', 1500, 60)
    view = nd('CompositorNodeViewer', 'ViewOut', 1500, -180)
    L(ealpha.outputs['Image'], comp.inputs['Image'])
    L(ealpha.outputs['Image'], view.inputs['Image'])

    # dithers the 8-bit output - the real cure for gradient banding on export
    scene.render.dither_intensity = 1.0
    return t


def cnodes():
    sc = bpy.context.scene
    return sc.node_tree.nodes if sc.use_nodes and sc.node_tree else None


# ========================================================================
#  PIPELINE B - physically simulated thin-film iridescence
# ========================================================================

IRI_MAT = "IridescentFilmMat"
GRAVITY = 9.81                 # m/s^2
SIGMA_OVER_RHO = 7.4e-5        # water surface tension / density, m^3/s^2


def simulate_loop(n=256, frames=120, domain=0.35, wind_speed=2.4,
                  wind_dir=(1.0, 0.45), capillary=1.0, seed=7, duration=4.0,
                  times=None):
    """Spectral gravity-capillary wave solver that loops exactly.

    Every Fourier mode is advanced with the real dispersion relation
    omega^2 = g*k + (sigma/rho)*k^3.  Snapping each omega to an integer
    multiple of the loop frequency makes every mode complete a whole number of
    cycles over `duration`, so t=duration reproduces t=0 bit for bit.
    """
    rng = np.random.default_rng(seed)
    idx = np.fft.fftfreq(n, d=1.0 / n)
    kx, ky = np.meshgrid(2.0 * np.pi * idx / domain,
                         2.0 * np.pi * idx / domain, indexing='xy')
    k = np.hypot(kx, ky)
    k_safe = np.where(k == 0, 1e-6, k)

    w = np.array(wind_dir, dtype=np.float64)
    norm = np.linalg.norm(w)
    w = w / norm if norm > 0 else np.array([1.0, 0.0])
    L_w = max(wind_speed, 1e-3) ** 2 / GRAVITY
    cos_term = ((kx / k_safe) * w[0] + (ky / k_safe) * w[1]) ** 2
    damp = np.exp(-k_safe ** 2 * (domain / n) ** 2)
    phillips = np.exp(-1.0 / (k_safe * L_w) ** 2) / k_safe ** 4 * cos_term * damp
    phillips[k == 0] = 0.0
    phillips = np.clip(phillips, 0.0, None)

    xi = rng.normal(size=(n, n)) + 1j * rng.normal(size=(n, n))
    h0 = xi * np.sqrt(phillips / 2.0)
    h0_conj = np.conj(np.roll(np.roll(h0[::-1, ::-1], 1, axis=0), 1, axis=1))

    omega = np.sqrt(GRAVITY * k_safe + capillary * SIGMA_OVER_RHO * k_safe ** 3)
    omega_0 = 2.0 * np.pi / max(duration, 1e-6)
    omega_q = np.round(omega / omega_0) * omega_0
    omega_q[k == 0] = 0.0

    ts = np.arange(frames) * duration / frames if times is None \
        else np.asarray(times, dtype=np.float64)
    out = np.empty((ts.size, n, n), dtype=np.float32)
    for f, t in enumerate(ts):
        phase = np.exp(1j * omega_q * t)
        hk = h0 * phase + h0_conj * np.conj(phase)
        out[f] = np.real(np.fft.ifft2(hk)) * (n * n)

    lo, hi = out.min(), out.max()
    if hi - lo < 1e-12:
        out[:] = 0.5
    else:
        out = (out - lo) / (hi - lo)
    return out.astype(np.float32)


def _cie_xyz(lam):
    """Wyman/Sloan/Shirley multi-lobe fits to the CIE 1931 2-degree observer."""
    def g(x, mu, s1, s2):
        s = np.where(x < mu, s1, s2)
        return np.exp(-0.5 * ((x - mu) / s) ** 2)
    x = 1.056 * g(lam, 599.8, 37.9, 31.0) + 0.362 * g(lam, 442.0, 16.0, 26.7) \
        - 0.065 * g(lam, 501.1, 20.4, 26.2)
    y = 0.821 * g(lam, 568.8, 46.9, 40.5) + 0.286 * g(lam, 530.9, 16.3, 31.1)
    z = 1.217 * g(lam, 437.0, 11.8, 36.0) + 0.681 * g(lam, 459.0, 26.0, 13.8)
    return x, y, z


def _fresnel(n_i, n_t, cos_i):
    sin_t2 = (n_i / n_t) ** 2 * (1.0 - cos_i ** 2)
    cos_t = np.sqrt(np.clip(1.0 - sin_t2, 0.0, 1.0))
    rs = (n_i * cos_i - n_t * cos_t) / (n_i * cos_i + n_t * cos_t)
    rp = (n_t * cos_i - n_i * cos_t) / (n_t * cos_i + n_i * cos_t)
    return rs, rp, cos_t


def thin_film_lut(samples=512, d_min=0.0, d_max=1400.0, n_film=1.35,
                  n_sub=1.0, angle_deg=0.0, n_air=1.0):
    """Colour as a function of film thickness in nm.

    Airy summation over the two interfaces gives spectral reflectance, which is
    integrated against the CIE colour matching functions and converted to linear
    sRGB. This is what produces the real Newton series (silver, gold, blue,
    magenta...) including the wash-out at large thickness.
    """
    lam = np.linspace(390.0, 750.0, 96)
    d = np.linspace(d_min, d_max, samples)[:, None]

    cos_i = np.cos(np.radians(angle_deg))
    r01s, r01p, cos_f = _fresnel(n_air, n_film, cos_i)
    r12s, r12p, _ = _fresnel(n_film, n_sub, cos_f)

    beta = 2.0 * np.pi * n_film * d * cos_f / lam[None, :]
    e = np.exp(-2j * beta)

    R = np.zeros((samples, lam.size))
    for r01, r12 in ((r01s, r12s), (r01p, r12p)):
        r = (r01 + r12 * e) / (1.0 + r01 * r12 * e)
        R += np.abs(r) ** 2
    R *= 0.5

    integrate = getattr(np, "trapezoid", None) or np.trapz
    xb, yb, zb = _cie_xyz(lam)
    yn = integrate(yb, lam)
    X = integrate(R * xb[None, :], lam, axis=1) / yn
    Y = integrate(R * yb[None, :], lam, axis=1) / yn
    Z = integrate(R * zb[None, :], lam, axis=1) / yn

    M = np.array([[3.2406, -1.5372, -0.4986],
                  [-0.9689, 1.8758, 0.0415],
                  [0.0557, -0.2040, 1.0570]])
    rgb = np.clip(np.stack([X, Y, Z], axis=1) @ M.T, 0.0, None)
    peak = rgb.max()
    if peak > 0:
        rgb /= peak
    return rgb.astype(np.float32)


# ------------------------------------------------------------ baking -------

def sim_dir():
    """Writable folder for baked frames - beside the .blend when saved."""
    if bpy.data.filepath:
        d = bpy.path.abspath("//wavetex_sim/")
    else:
        d = os.path.join(bpy.app.tempdir, "wavetex_sim")
    os.makedirs(d, exist_ok=True)
    return d


class _ImageSettings:
    """Snapshot/restore scene output settings.

    Baking has to flip the output to single-channel EXR. Leaving that in place
    silently renders every later frame in greyscale, so restoring is mandatory.
    """
    FIELDS = ('file_format', 'color_mode', 'color_depth', 'exr_codec', 'compression')

    def __init__(self, scene):
        self.s = scene.render.image_settings

    def __enter__(self):
        self.saved = {f: getattr(self.s, f) for f in self.FIELDS}
        return self.s

    def __exit__(self, *exc):
        for f, v in self.saved.items():
            try:
                setattr(self.s, f, v)
            except Exception:
                pass
        return False


def _write_exr(scene, name, arr, path, mode='BW', depth='16'):
    h, w = arr.shape[:2]
    img = bpy.data.images.get(name)
    if img:
        bpy.data.images.remove(img)
    img = bpy.data.images.new(name, width=w, height=h, alpha=False, float_buffer=True)
    img.colorspace_settings.name = 'Non-Color'
    rgba = np.empty((h, w, 4), dtype=np.float32)
    if arr.ndim == 2:
        rgba[:, :, 0] = rgba[:, :, 1] = rgba[:, :, 2] = arr
    else:
        rgba[:, :, :3] = arr
    rgba[:, :, 3] = 1.0
    img.pixels.foreach_set(rgba.ravel())
    img.save_render(path, scene=scene)
    bpy.data.images.remove(img)


def bake_simulation(scene, report=None):
    p = scene.wavetex
    d = sim_dir()
    for f in glob.glob(os.path.join(d, "height_*.exr")):
        os.remove(f)

    frames = p.loop_frames
    height = simulate_loop(n=int(p.sim_res), frames=frames, domain=p.sim_domain,
                           wind_speed=p.sim_wind, capillary=p.sim_capillary,
                           wind_dir=(math.cos(math.radians(p.sim_wind_angle)),
                                     math.sin(math.radians(p.sim_wind_angle))),
                           seed=p.sim_seed, duration=p.sim_duration)

    with _ImageSettings(scene) as s:
        s.file_format, s.color_mode, s.color_depth, s.exr_codec = 'OPEN_EXR', 'BW', '16', 'ZIP'
        for i in range(frames):
            _write_exr(scene, "_wt_bake", height[i],
                       os.path.join(d, "height_%04d.exr" % (i + 1)))

    img = bpy.data.images.get("WT_SimHeight")
    if img:
        bpy.data.images.remove(img)
    img = bpy.data.images.load(os.path.join(d, "height_0001.exr"))
    img.name = "WT_SimHeight"
    img.source = 'SEQUENCE'
    img.colorspace_settings.name = 'Non-Color'
    if report:
        report({'INFO'}, "Baked %d frames at %dpx" % (frames, int(p.sim_res)))
    return img


def bake_lut(scene, report=None):
    p = scene.wavetex
    d = sim_dir()
    lut = thin_film_lut(samples=512, d_max=p.film_thickness_max, n_film=p.film_ior,
                        n_sub=p.substrate_ior, angle_deg=p.film_angle)
    path = os.path.join(d, "film_lut.exr")
    with _ImageSettings(scene) as s:
        s.file_format, s.color_mode, s.color_depth, s.exr_codec = 'OPEN_EXR', 'RGB', '32', 'ZIP'
        _write_exr(scene, "_wt_lut", np.repeat(lut[None, :, :], 8, axis=0), path)

    img = bpy.data.images.get("WT_FilmLUT")
    if img:
        bpy.data.images.remove(img)
    img = bpy.data.images.load(path)
    img.name = "WT_FilmLUT"
    img.colorspace_settings.name = 'Non-Color'
    if report:
        report({'INFO'}, "Film LUT rebuilt (IOR %.2f / %.2f)" % (p.film_ior, p.substrate_ior))
    return img


def build_iridescent_material(scene):
    mat = bpy.data.materials.get(IRI_MAT) or bpy.data.materials.new(IRI_MAT)
    # Fake user, or Blender drops this material on save whenever another
    # pipeline is the one assigned to the plane - zero users means purged.
    mat.use_fake_user = True
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    N, L = nt.nodes, nt.links.new

    def nd(kind, name, x, y):
        n = N.new(kind)
        n.name = n.label = name
        n.location = (x, y)
        return n

    texco = nd('ShaderNodeTexCoord', 'TexCo', -2000, 0)
    cen = nd('ShaderNodeVectorMath', 'CenterCoord', -1840, 0)
    cen.operation = 'SUBTRACT'
    cen.inputs[1].default_value = (0.5, 0.5, 0.0)
    L(texco.outputs['Generated'], cen.inputs[0])
    bmap = nd('ShaderNodeMapping', 'BaseMap', -1680, 0)
    L(cen.outputs[0], bmap.inputs['Vector'])

    phase = nd('ShaderNodeValue', 'IriPhase', -2000, 360)
    drv = phase.outputs[0].driver_add('default_value').driver
    drv.type = 'SCRIPTED'
    drv.expression = "frame / 120 * %s" % TAU

    # baked simulation, played back cyclically
    simmap = nd('ShaderNodeMapping', 'SimMap', -1500, -320)
    L(bmap.outputs['Vector'], simmap.inputs['Vector'])
    sim = nd('ShaderNodeTexImage', 'SimHeight', -1320, -320)
    sim.image = bpy.data.images.get("WT_SimHeight")
    sim.extension = 'REPEAT'
    iu = sim.image_user
    iu.frame_duration = scene.wavetex.loop_frames
    iu.frame_start = 1
    iu.use_cyclic = True
    iu.use_auto_refresh = True
    L(simmap.outputs['Vector'], sim.inputs['Vector'])

    # film thickness = base + sim relief + a travelling sweep
    hc = nd('ShaderNodeMath', 'HeightCenter', -1080, -220)
    hc.operation = 'SUBTRACT'
    hc.inputs[1].default_value = 0.5
    L(sim.outputs['Color'], hc.inputs[0])
    hg = nd('ShaderNodeMath', 'HeightGain', -920, -220)
    hg.operation = 'MULTIPLY'
    hg.inputs[1].default_value = 0.30
    L(hc.outputs[0], hg.inputs[0])

    sdot = nd('ShaderNodeVectorMath', 'SweepDot', -1500, 200)
    sdot.operation = 'DOT_PRODUCT'
    sdot.inputs[1].default_value = (1.0, 0.7, 0.0)
    L(bmap.outputs['Vector'], sdot.inputs[0])
    sf = nd('ShaderNodeMath', 'SweepFreq', -1340, 200)
    sf.operation = 'MULTIPLY'
    sf.inputs[1].default_value = 3.0
    L(sdot.outputs['Value'], sf.inputs[0])
    sp = nd('ShaderNodeMath', 'SweepPhase', -1180, 200)
    sp.operation = 'ADD'
    L(sf.outputs[0], sp.inputs[0])
    L(phase.outputs[0], sp.inputs[1])
    ss = nd('ShaderNodeMath', 'SweepSin', -1020, 200)
    ss.operation = 'SINE'
    L(sp.outputs[0], ss.inputs[0])
    sa = nd('ShaderNodeMath', 'SweepAmp', -860, 200)
    sa.operation = 'MULTIPLY'
    sa.inputs[1].default_value = 0.38
    L(ss.outputs[0], sa.inputs[0])

    tsum = nd('ShaderNodeMath', 'ThickSum', -700, 0)
    tsum.operation = 'ADD'
    L(hg.outputs[0], tsum.inputs[0])
    L(sa.outputs[0], tsum.inputs[1])
    tb = nd('ShaderNodeMath', 'ThickBase', -540, 0)
    tb.operation = 'ADD'
    tb.inputs[1].default_value = 0.35
    tb.use_clamp = True
    L(tsum.outputs[0], tb.inputs[0])

    lc = nd('ShaderNodeCombineXYZ', 'LUTCoord', -380, 0)
    lc.inputs['Y'].default_value = 0.5
    L(tb.outputs[0], lc.inputs['X'])
    film = nd('ShaderNodeTexImage', 'FilmLUT', -220, 0)
    film.image = bpy.data.images.get("WT_FilmLUT")
    film.extension = 'EXTEND'
    film.interpolation = 'Linear'
    L(lc.outputs[0], film.inputs['Vector'])

    # substrate: stretched fibres + fine tooth
    pmap = nd('ShaderNodeMapping', 'PaperMap', -1500, -750)
    pmap.inputs['Scale'].default_value = (1.0, 0.18, 1.0)
    L(bmap.outputs['Vector'], pmap.inputs['Vector'])
    fib = nd('ShaderNodeTexNoise', 'PaperFibre', -1320, -750)
    fib.inputs['Scale'].default_value = 90.0
    fib.inputs['Detail'].default_value = 6.0
    L(pmap.outputs[0], fib.inputs['Vector'])
    tooth = nd('ShaderNodeTexNoise', 'PaperTooth', -1320, -980)
    tooth.inputs['Scale'].default_value = 260.0
    tooth.inputs['Detail'].default_value = 3.0
    L(bmap.outputs['Vector'], tooth.inputs['Vector'])
    gm = nd('ShaderNodeMix', 'GrainMix', -1120, -860)
    gm.data_type = 'FLOAT'
    gm.inputs['Factor'].default_value = 0.5
    L(fib.outputs['Fac'], gm.inputs['A'])
    L(tooth.outputs['Fac'], gm.inputs['B'])
    gr = nd('ShaderNodeMapRange', 'GrainRange', -960, -860)
    gr.inputs['To Min'].default_value = 0.82
    gr.inputs['To Max'].default_value = 1.18
    L(gm.outputs['Result'], gr.inputs['Value'])
    sc_col = nd('ShaderNodeRGB', 'SubstrateColor', -960, -1090)
    sc_col.outputs[0].default_value = (0.045, 0.20, 0.38, 1.0)
    smul = nd('ShaderNodeMix', 'SubstrateMul', -780, -960)
    smul.data_type = 'RGBA'
    smul.blend_type = 'MULTIPLY'
    smul.inputs['Factor'].default_value = 1.0
    L(sc_col.outputs[0], smul.inputs[6])
    L(gr.outputs['Result'], smul.inputs[7])

    # where the film sits - a band that travels with the loop
    cdot = nd('ShaderNodeVectorMath', 'CoverDot', -1500, 560)
    cdot.operation = 'DOT_PRODUCT'
    cdot.inputs[1].default_value = (1.0, 0.7, 0.0)
    L(bmap.outputs['Vector'], cdot.inputs[0])
    cf = nd('ShaderNodeMath', 'CoverFreq', -1340, 560)
    cf.operation = 'MULTIPLY'
    cf.inputs[1].default_value = 0.7
    L(cdot.outputs['Value'], cf.inputs[0])
    cp = nd('ShaderNodeMath', 'CoverPhase', -1180, 560)
    cp.operation = 'ADD'
    L(cf.outputs[0], cp.inputs[0])
    L(phase.outputs[0], cp.inputs[1])
    cs = nd('ShaderNodeMath', 'CoverSin', -1020, 560)
    cs.operation = 'SINE'
    L(cp.outputs[0], cs.inputs[0])
    cr = nd('ShaderNodeMapRange', 'CoverRange', -860, 560)
    cr.inputs['From Min'].default_value = 0.15
    cr.inputs['From Max'].default_value = 1.0
    cr.clamp = True
    L(cs.outputs[0], cr.inputs['Value'])
    cst = nd('ShaderNodeMath', 'CoverStrength', -700, 560)
    cst.operation = 'MULTIPLY'
    cst.inputs[1].default_value = 1.0
    cst.use_clamp = True
    L(cr.outputs['Result'], cst.inputs[0])

    fg = nd('ShaderNodeMix', 'FilmGain', -220, -400)
    fg.data_type = 'RGBA'
    fg.blend_type = 'MULTIPLY'
    fg.inputs['Factor'].default_value = 1.0
    fg.inputs[7].default_value = (0.95, 0.95, 0.95, 1.0)
    L(film.outputs['Color'], fg.inputs[6])
    fm = nd('ShaderNodeMix', 'FilmMasked', -40, -400)
    fm.data_type = 'RGBA'
    fm.inputs[6].default_value = (0.0, 0.0, 0.0, 1.0)
    L(cst.outputs[0], fm.inputs['Factor'])
    L(fg.outputs[2], fm.inputs[7])
    comb = nd('ShaderNodeMix', 'FilmOverSubstrate', 160, -200)
    comb.data_type = 'RGBA'
    comb.blend_type = 'ADD'
    comb.inputs['Factor'].default_value = 1.0
    L(smul.outputs[2], comb.inputs[6])
    L(fm.outputs[2], comb.inputs[7])

    bump = nd('ShaderNodeBump', 'IriBump', 160, -620)
    bump.inputs['Strength'].default_value = 0.25
    L(sim.outputs['Color'], bump.inputs['Height'])
    emi = nd('ShaderNodeEmission', 'IriEmission', 380, 80)
    L(comb.outputs[2], emi.inputs['Color'])
    pb = nd('ShaderNodeBsdfPrincipled', 'IriPBSDF', 380, -240)
    pb.inputs['Roughness'].default_value = 0.35
    L(comb.outputs[2], pb.inputs['Base Color'])
    L(bump.outputs['Normal'], pb.inputs['Normal'])
    mix = nd('ShaderNodeMixShader', 'IriShadeMix', 640, -60)
    mix.inputs['Fac'].default_value = 0.0
    L(emi.outputs['Emission'], mix.inputs[1])
    L(pb.outputs['BSDF'], mix.inputs[2])
    out = nd('ShaderNodeOutputMaterial', 'IriOut', 840, -60)
    L(mix.outputs['Shader'], out.inputs['Surface'])
    return mat


def inodes():
    mat = bpy.data.materials.get(IRI_MAT)
    return mat.node_tree.nodes if mat else None


# ========================================================================
#  PIPELINE 3 - AURA FLOW
#  Domain-warped fBm. A wave texture can only make bands, which is why the
#  wave pipeline reads as a striped sweep however far you push it. Warping the
#  sample coordinates by noise, twice, gives large soft blobs with no visible
#  periodicity - the form real gradient artwork uses.
#
#  Looping: each warp layer samples its noise at a point travelling round a
#  circle, so one phase cycle returns to the exact starting coordinate. The
#  layers orbit at different radii and phase offsets, so the field morphs
#  instead of sliding rigidly - slow organic drift that still closes perfectly.
# ========================================================================

AURA_MAT = "AuraFlowMat"
# Bump whenever the node set changes. A .blend saved with an older graph then
# opened against newer code would otherwise raise KeyError inside an update
# callback, and Blender swallows those - the panel silently stops working.
AURA_VERSION = 2


def anodes():
    mat = bpy.data.materials.get(AURA_MAT)
    if mat is None:
        return None
    if mat.get("wt_aura_version", 0) != AURA_VERSION:
        build_aura_material()
        mat = bpy.data.materials.get(AURA_MAT)
        try:
            u_colors(bpy.context.scene.wavetex, bpy.context)
        except Exception:
            pass
    return mat.node_tree.nodes


def build_aura_material():
    mat = bpy.data.materials.get(AURA_MAT) or bpy.data.materials.new(AURA_MAT)
    # Fake user, or Blender drops this material on save whenever another
    # pipeline is the one assigned to the plane - zero users means purged.
    mat.use_fake_user = True
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    n, L = nt.nodes, nt.links.new

    def nd(kind, name, x, y):
        node = n.new(kind)
        node.name = node.label = name
        node.location = (x, y)
        return node

    phase = nd('ShaderNodeValue', 'PhaseValue', -2100, 400)

    def orbit(idx, radius, offset, x, y):
        """A point circling the origin - the reason this loops."""
        add = nd('ShaderNodeMath', 'OrbPhase%d' % idx, x, y)
        add.operation = 'ADD'
        add.inputs[1].default_value = offset
        L(phase.outputs[0], add.inputs[0])
        c = nd('ShaderNodeMath', 'OrbCos%d' % idx, x + 170, y + 60)
        c.operation = 'COSINE'
        s = nd('ShaderNodeMath', 'OrbSin%d' % idx, x + 170, y - 110)
        s.operation = 'SINE'
        L(add.outputs[0], c.inputs[0])
        L(add.outputs[0], s.inputs[0])
        cm = nd('ShaderNodeMath', 'OrbCosR%d' % idx, x + 340, y + 60)
        cm.operation = 'MULTIPLY'
        cm.inputs[1].default_value = radius
        sm = nd('ShaderNodeMath', 'OrbSinR%d' % idx, x + 340, y - 110)
        sm.operation = 'MULTIPLY'
        sm.inputs[1].default_value = radius
        L(c.outputs[0], cm.inputs[0])
        L(s.outputs[0], sm.inputs[0])
        vec = nd('ShaderNodeCombineXYZ', 'Orbit%d' % idx, x + 510, y - 30)
        L(cm.outputs[0], vec.inputs['X'])
        L(sm.outputs[0], vec.inputs['Y'])
        return vec

    o1 = orbit(1, 0.22, 0.0, -2100, 60)
    o2 = orbit(2, 0.15, 2.1, -2100, -330)
    o3 = orbit(3, 0.09, 4.2, -2100, -720)

    texco = nd('ShaderNodeTexCoord', 'TexCo', -2100, 900)
    centre = nd('ShaderNodeVectorMath', 'CenterCoord', -1900, 900)
    centre.operation = 'SUBTRACT'
    centre.inputs[1].default_value = (0.5, 0.5, 0.0)
    L(texco.outputs['Generated'], centre.inputs[0])
    mapn = nd('ShaderNodeMapping', 'AuraMap', -1700, 900)
    mapn.inputs['Scale'].default_value = (1.0, 1.0, 1.0)
    L(centre.outputs[0], mapn.inputs['Vector'])

    # Sampling the field at a different origin gives a different composition
    # without touching feature size - otherwise every render is the same
    # cool-corner/warm-field diagonal.
    soff = nd('ShaderNodeCombineXYZ', 'SeedOffset', -1520, 1080)
    base = nd('ShaderNodeVectorMath', 'SeedAdd', -1520, 900)
    base.operation = 'ADD'
    L(mapn.outputs[0], base.inputs[0])
    L(soff.outputs[0], base.inputs[1])

    def warp(idx, src, orb, x, scale, amount):
        add = nd('ShaderNodeVectorMath', 'WarpIn%d' % idx, x, 700)
        add.operation = 'ADD'
        L(src.outputs[0], add.inputs[0])
        L(orb.outputs[0], add.inputs[1])
        nz = nd('ShaderNodeTexNoise', 'WarpNoise%d' % idx, x + 180, 700)
        nz.inputs['Scale'].default_value = scale
        nz.inputs['Detail'].default_value = 2.0
        nz.inputs['Roughness'].default_value = 0.5
        L(add.outputs[0], nz.inputs['Vector'])
        cen = nd('ShaderNodeVectorMath', 'WarpCenter%d' % idx, x + 360, 700)
        cen.operation = 'SUBTRACT'
        cen.inputs[1].default_value = (0.5, 0.5, 0.5)
        L(nz.outputs['Color'], cen.inputs[0])
        mul = nd('ShaderNodeVectorMath', 'WarpAmt%d' % idx, x + 540, 700)
        mul.operation = 'SCALE'
        mul.inputs['Scale'].default_value = amount
        L(cen.outputs[0], mul.inputs[0])
        out = nd('ShaderNodeVectorMath', 'WarpOut%d' % idx, x + 720, 700)
        out.operation = 'ADD'
        L(src.outputs[0], out.inputs[0])
        L(mul.outputs[0], out.inputs[1])
        return out

    w1 = warp(1, base, o1, -1450, 1.6, 0.55)
    w2 = warp(2, w1, o2, -450, 3.1, 0.28)

    fin = nd('ShaderNodeVectorMath', 'FieldIn', 560, 700)
    fin.operation = 'ADD'
    L(w2.outputs[0], fin.inputs[0])
    L(o3.outputs[0], fin.inputs[1])
    field = nd('ShaderNodeTexNoise', 'FieldNoise', 740, 700)
    field.inputs['Scale'].default_value = 1.1
    field.inputs['Detail'].default_value = 3.0
    field.inputs['Roughness'].default_value = 0.45
    L(fin.outputs[0], field.inputs['Vector'])

    # Contrast shaping: pushes the field toward its extremes so blobs read as
    # distinct colour territories rather than an even wash.
    shape = nd('ShaderNodeMapRange', 'FieldShape', 940, 700)
    shape.inputs['From Min'].default_value = 0.30
    shape.inputs['From Max'].default_value = 0.70
    shape.clamp = True
    L(field.outputs['Fac'], shape.inputs['Value'])
    _ = shape

    # Histogram equaliser. Blender's noise piles up around 0.5, so uniform
    # quantiser buckets hand almost no area to the outer zones and palette
    # colours silently vanish from the render. This ramp is baked from the
    # field's measured CDF by wavetex.balance_zones, which flattens the
    # distribution so every zone gets the area it is supposed to have.
    eq = nd('ShaderNodeValToRGB', 'FieldEqualise', 1130, 380)
    eq.color_ramp.interpolation = 'LINEAR'
    eq.color_ramp.elements[0].position = 0.0
    eq.color_ramp.elements[0].color = (0, 0, 0, 1)
    eq.color_ramp.elements[1].position = 1.0
    eq.color_ramp.elements[1].color = (1, 1, 1, 1)
    L(shape.outputs['Result'], eq.inputs['Fac'])

    # Optional quantiser - the aura look steps between hue families instead of
    # interpolating through them. Blur afterwards keeps the edges soft.
    steps = nd('ShaderNodeMapRange', 'FieldSteps', 1130, 700)
    steps.interpolation_type = 'STEPPED'
    steps.inputs['Steps'].default_value = 0.0
    steps.clamp = True
    L(eq.outputs['Color'], steps.inputs['Value'])

    ramp = nd('ShaderNodeValToRGB', 'GradientRamp', 1320, 700)
    ramp.color_ramp.interpolation = 'EASE'
    L(steps.outputs['Result'], ramp.inputs['Fac'])

    emit = nd('ShaderNodeEmission', 'AuraEmission', 1650, 700)
    emit.inputs['Strength'].default_value = 1.0
    L(ramp.outputs['Color'], emit.inputs['Color'])
    out = nd('ShaderNodeOutputMaterial', 'AuraOut', 1840, 700)
    L(emit.outputs['Emission'], out.inputs['Surface'])

    phase.outputs[0].driver_remove('default_value')
    drv = phase.outputs[0].driver_add('default_value').driver
    drv.type = 'SCRIPTED'
    drv.expression = "frame / 480 * %s" % TAU
    mat["wt_aura_version"] = AURA_VERSION
    return mat


def assign_pipeline(ctx):
    plane = bpy.data.objects.get(PLANE_NAME)
    if not plane:
        return
    pl = ctx.scene.wavetex.pipeline
    name = {'IRIDESCENT': IRI_MAT, 'AURA': AURA_MAT}.get(pl, MAT_NAME)
    mat = bpy.data.materials.get(name)
    if not mat:
        return
    plane.data.materials.clear()
    plane.data.materials.append(mat)


# ------------------------------------------------------------- callbacks ----

def u_style(self, ctx):
    w = nodes()['WaveTex']
    if self.wave_style == 'WATER':
        w.wave_type = 'BANDS'
        w.bands_direction = 'DIAGONAL'
    else:
        w.wave_type = 'RINGS'
        w.rings_direction = 'SPHERICAL'
    w.wave_profile = 'SIN'


def u_wave(self, ctx):
    n = nodes()
    w = n['WaveTex']
    w.inputs['Scale'].default_value = self.wave_scale
    w.inputs['Distortion'].default_value = self.distortion
    w.inputs['Detail'].default_value = self.detail
    n['SpeedMul'].inputs[1].default_value = float(self.speed)


def u_colors(self, ctx):
    if self.palette_mode == 'BRAND':
        cols = [tuple(self.brand_1), tuple(self.brand_2), tuple(self.brand_3),
                tuple(self.brand_4)][:max(2, self.brand_count)]
        pal = brand_palette(cols, self.color_stops, self.brand_lift)
        if self.brightness != 1.0 or self.saturation != 1.0:
            adj = []
            for r, g, b, a in pal:
                h, s, v = colorsys.rgb_to_hsv(r, g, b)
                r2, g2, b2 = colorsys.hsv_to_rgb(h, min(1.0, s * self.saturation),
                                                 min(1.0, v * self.brightness))
                adj.append((r2, g2, b2, a))
            pal = adj
        apply_palette(pal)
    else:
        apply_palette(gen_palette(self.seed, self.harmony, self.saturation,
                                  self.brightness, self.color_stops))


def u_interp(self, ctx):
    nodes()['GradientRamp'].color_ramp.interpolation = self.gradient_interp


def u_facshape(self, ctx):
    n = nodes()
    n['FacGamma'].inputs[1].default_value = self.band_sharpness
    n['FacCycles'].inputs[1].default_value = self.color_cycles


def u_noise(self, ctx):
    n = nodes()
    n['FacMix'].inputs['Factor'].default_value = self.noise_amount
    n['NoiseTex'].inputs['Scale'].default_value = self.noise_scale
    n['NoiseTex'].inputs['Detail'].default_value = self.noise_detail
    n['CosMul'].inputs[1].default_value = self.noise_drift
    n['SinMul'].inputs[1].default_value = self.noise_drift


def u_overlay(self, ctx):
    mat = bpy.data.materials[MAT_NAME]
    n, links = mat.node_tree.nodes, mat.node_tree.links
    mix = n['OverlayMix']
    for lk in list(mix.inputs['B'].links):
        links.remove(lk)
    if self.overlay_type == 'NONE':
        mix.inputs['Factor'].default_value = 0.0
    else:
        if self.overlay_type == 'MAGIC':
            links.new(n['BigOverlayMagic'].outputs['Color'], mix.inputs['B'])
        else:
            links.new(n['BigOverlayVoronoi'].outputs['Distance'], mix.inputs['B'])
        mix.inputs['Factor'].default_value = self.overlay_amount
    n['BigOverlayMagic'].inputs['Scale'].default_value = self.overlay_scale
    n['BigOverlayVoronoi'].inputs['Scale'].default_value = self.overlay_scale


def u_surface(self, ctx):
    n = nodes()
    n['BumpNode'].inputs['Strength'].default_value = self.bump_strength
    pb = n['PBSDF']
    pb.inputs['Roughness'].default_value = self.roughness
    pb.inputs['Metallic'].default_value = self.metallic
    u_shade(self, ctx)


def u_shade(self, ctx):
    n = nodes()
    flat = self.shading_mode == 'FLAT'
    n['ShadeMix'].inputs['Fac'].default_value = 0.0 if flat else 1.0
    n['FlatEmission'].inputs['Strength'].default_value = self.emission
    n['PBSDF'].inputs['Emission Strength'].default_value = self.emission


def u_filter(self, ctx):
    n = nodes()
    hs = n['FilterHueSat']
    hs.inputs['Hue'].default_value = (0.5 + self.hue_shift) % 1.0
    hs.inputs['Saturation'].default_value = self.filter_sat
    hs.inputs['Value'].default_value = self.filter_value
    bc = n['FilterContrast']
    bc.inputs['Contrast'].default_value = self.contrast
    bc.inputs['Bright'].default_value = self.filter_bright
    t = n['FilterTint']
    t.inputs['Factor'].default_value = self.tint_amount
    t.inputs[7].default_value = tuple(self.tint_color) + (1.0,)


def u_transform(self, ctx):
    b = nodes()['BaseMap']
    b.inputs['Rotation'].default_value[2] = math.radians(self.rotation)
    b.inputs['Location'].default_value = (self.offset_x, self.offset_y, 0.0)
    b.inputs['Scale'].default_value = (self.stretch_x, self.stretch_y, 1.0)


def u_loop(self, ctx):
    sc = ctx.scene
    sc.frame_start = 1
    sc.frame_end = self.loop_frames
    for name in (MAT_NAME, IRI_MAT, AURA_MAT):
        mat = bpy.data.materials.get(name)
        if not mat or not mat.node_tree.animation_data:
            continue
        for fc in mat.node_tree.animation_data.drivers:
            fc.driver.expression = "frame / %d * %s" % (self.loop_frames, TAU)
    n = inodes()
    if n:
        # the baked sequence must span exactly one loop or playback drifts
        n['SimHeight'].image_user.frame_duration = self.loop_frames


def u_loop_seconds(self, ctx):
    fps = ctx.scene.render.fps / ctx.scene.render.fps_base
    frames = int(round(self.loop_seconds * fps))
    frames = min(600, max(24, frames))
    if frames != self.loop_frames:
        self.loop_frames = frames        # its own update rewires the drivers


def u_view(self, ctx):
    ctx.scene.view_settings.view_transform = self.view_transform
    ctx.scene.view_settings.look = 'None'


# ----------------------------------------------------- effect callbacks -----

def u_fx_lens(self, ctx):
    n = cnodes()
    if not n:
        return
    on = self.use_lens
    n['FX_Lens'].inputs['Dispersion'].default_value = self.chromatic if on else 0.0
    n['FX_Lens'].inputs['Distortion'].default_value = self.lens_distort if on else 0.0


def _short_edge(ctx):
    r = ctx.scene.render
    return min(r.resolution_x, r.resolution_y) * r.resolution_percentage / 100.0


def u_fx_blur(self, ctx):
    n = cnodes()
    if not n:
        return
    # Size is in pixels, so a percent-of-image slider has to be resolved against
    # the current output resolution (and re-resolved when it changes).
    amt = self.blur_amount if self.use_blur else 0.0
    px = amt / 100.0 * _short_edge(ctx)
    b = n['FX_Blur']
    b.inputs['Size'].default_value = (px, px)
    b.mute = px <= 0.0
    k = n['FX_Painterly']
    k.mute = (not self.use_blur) or self.painterly <= 0.0
    k.inputs['Size'].default_value = max(1.0, self.painterly)
    px = n['FX_Pixelate']
    px.mute = (not self.use_blur) or self.pixelate <= 1
    px.inputs['Size'].default_value = int(max(1, self.pixelate))


def u_fx_bloom(self, ctx):
    n = cnodes()
    if not n:
        return
    g = n['FX_Bloom']
    g.mute = (not self.use_bloom) or self.bloom <= 0.0
    g.inputs['Strength'].default_value = self.bloom if self.use_bloom else 0.0
    g.inputs['Threshold'].default_value = self.bloom_threshold
    g.inputs['Size'].default_value = self.bloom_size
    # the socket alone does not drive BLOOM in 4.5 - the legacy int still rules
    g.size = int(self.bloom_size)


def u_fx_dither(self, ctx):
    n = cnodes()
    if not n:
        return
    post = n['FX_Posterize']
    post.mute = (not self.use_dither) or self.posterize_steps <= 0
    steps = max(2, self.posterize_steps)
    post.inputs['Steps'].default_value = float(steps)

    dp = n['DitherPattern']
    if self.dither_mode == 'NONE' or not self.use_dither:
        n['DitherAdd'].inputs['Fac'].default_value = 0.0
        return
    dp.image = bpy.data.images.get(
        "WT_DitherBayer" if self.dither_mode == 'ORDERED' else "WT_DitherNoise")
    # amplitude of one quantisation step, so the dither exactly spans a band
    if self.posterize_steps > 0:
        fac = self.dither_amount * 2.0 / steps
    else:
        # Anti-banding only. Measured on real sites this sits near 0.8% sigma;
        # the pattern is +/-0.5 so 0.012 lands there at amount 1.0. Anything
        # stronger reads as noise once sRGB expands the shadows.
        fac = self.dither_amount * 0.012
    n['DitherAdd'].inputs['Fac'].default_value = fac


def u_grain_build(self, ctx):
    """Grain geometry changed, so the tile has to be regenerated."""
    r = ctx.scene.render
    if r.resolution_x < 4 or r.resolution_y < 4:
        return
    _pattern_image("WT_Grain", build_grain(
        r.resolution_x, r.resolution_y, size=self.grain_size,
        roughness=self.grain_roughness, scale=self.grain_scale,
        chroma=self.grain_chroma, seed=self.seed))
    u_fx_grain(self, ctx)


def u_fx_grain(self, ctx):
    n = cnodes()
    if not n:
        return
    n['GrainAdd'].inputs['Fac'].default_value = self.grain if self.use_grain else 0.0
    # Blend the density curve toward flat as rolloff drops. 1.0 is the film
    # curve (quiet shadows and highlights, peak in the midtones); 0.0 is
    # uniform digital noise everywhere.
    k = self.grain_rolloff
    curve = [(0.00, 0.10), (0.10, 0.45), (0.35, 1.00), (0.65, 0.90), (1.00, 0.22)]
    _ramp_set(n['GrainAmp'],
              [(pos, tuple([v * k + 1.0 * (1.0 - k)] * 3) + (1.0,)) for pos, v in curve],
              'LINEAR')
    # generated images do not survive a .blend round-trip, so re-point the node
    n['GrainImage'].image = bpy.data.images.get("WT_Grain")
    tr = n['GrainTranslate']
    _grain_offsets(self, ctx, tr)


def _grain_offsets(self, ctx, tr):
    """Give every frame its own unrelated grain offset.

    Baked as constant-interpolation keyframes rather than a driver expression.
    Blender's restricted driver evaluator silently returns 0 for anything it
    cannot parse - no error, no warning - and the arithmetic needed to
    decorrelate successive frames is past what it accepts. Keyframes are
    unambiguous and cost nothing at this length.
    """
    nt = tr.id_data
    for sock in ('X', 'Y'):
        try:
            tr.inputs[sock].driver_remove('default_value')
        except Exception:
            pass
    idx = {s.name: i for i, s in enumerate(tr.inputs)}
    paths = {sock: 'nodes["%s"].inputs[%d].default_value' % (tr.name, idx[sock])
             for sock in ('X', 'Y')}
    if nt.animation_data and nt.animation_data.action:
        for fc in list(nt.animation_data.action.fcurves):
            if fc.data_path in paths.values():
                nt.animation_data.action.fcurves.remove(fc)

    if not self.grain_animate:
        tr.inputs['X'].default_value = 0.0
        tr.inputs['Y'].default_value = 0.0
        return

    w, h = ctx.scene.render.resolution_x, ctx.scene.render.resolution_y
    rng = random.Random(self.seed * 7919 + 17)
    spans = {'X': w, 'Y': h}
    for f in range(1, self.loop_frames + 1):
        for sock in ('X', 'Y'):
            # whole pixels only - a fractional shift asks the compositor to
            # resample a tile whose whole point is per-pixel detail
            tr.inputs[sock].default_value = float(rng.randrange(spans[sock]))
        for sock in ('X', 'Y'):
            tr.inputs[sock].keyframe_insert('default_value', frame=f)
    if nt.animation_data and nt.animation_data.action:
        for fc in nt.animation_data.action.fcurves:
            if fc.data_path in paths.values():
                # CONSTANT, or Blender interpolates between offsets and the
                # grain slides again - the exact bug this replaces
                for kp in fc.keyframe_points:
                    kp.interpolation = 'CONSTANT'
                fc.update()


def u_fx_vignette(self, ctx):
    n = cnodes()
    if not n:
        return
    n['VignetteMul'].inputs['Fac'].default_value = self.vignette if self.use_vignette else 0.0
    n['VignetteMask'].inputs['Size'].default_value = (self.vignette_size, self.vignette_size)
    px = max(1.0, self.vignette_softness / 100.0 * _short_edge(ctx))
    n['VignetteBlur'].inputs['Size'].default_value = (px, px)


def u_fx_exposure(self, ctx):
    n = cnodes()
    if not n:
        return
    n['FX_Exposure'].inputs['Exposure'].default_value = self.exposure


def u_fx_tone(self, ctx):
    n = cnodes()
    if not n:
        return
    lo, hi = sorted((self.tone_floor, self.tone_ceiling))
    span = max(0.0, hi - lo)
    off = (lo <= 0.001 and hi >= 0.999)
    scale = n['ToneScale']
    scale.inputs[2].default_value = (span, span, span, 1.0)
    scale.mute = off
    if not self.use_tone:
        lo, hi = 0.0, 1.0
    t = n['FX_ToneRange']
    t.inputs[2].default_value = (lo, lo, lo, 1.0)
    t.mute = off


def u_fx_scrim(self, ctx):
    """Scrim and edge-fade masks are baked images, so a shape change means a
    rebuild - cheap, but it has to happen before the nodes are re-pointed."""
    r = ctx.scene.render
    build_patterns(r.resolution_x, r.resolution_y,
                   scrim=(self.scrim_dir, self.scrim_coverage, self.scrim_softness),
                   edge=(self.edge_inset, self.edge_softness),
                   grain=dict(size=self.grain_size, roughness=self.grain_roughness,
                              scale=self.grain_scale, chroma=self.grain_chroma))
    n = cnodes()
    if not n:
        return
    n['ScrimImage'].image = bpy.data.images.get("WT_Scrim")
    n['ScrimStrength'].inputs[1].default_value = self.scrim_strength if self.use_scrim else 0.0
    n['ScrimColor'].outputs[0].default_value = tuple(self.scrim_color) + (1.0,)
    n['EdgeImage'].image = bpy.data.images.get("WT_EdgeFade")
    # Edge fade writes ALPHA. With an opaque film the alpha is discarded on
    # save, so the control looks dead - gate it on transparency and surface
    # that dependency in the panel rather than failing silently.
    n['FX_EdgeAlpha'].mute = not (self.edge_fade and self.transparent_bg)


def u_viewport_fx(self, ctx):
    for a in ctx.screen.areas:
        if a.type == 'VIEW_3D':
            for s in a.spaces:
                if s.type == 'VIEW_3D':
                    s.shading.use_compositor = self.viewport_fx


def u_transparent(self, ctx):
    ctx.scene.render.film_transparent = self.transparent_bg
    # An RGB output silently discards alpha, which makes both the transparent
    # film and the edge fade look broken - they were working, the save was
    # throwing the channel away.
    ctx.scene.render.image_settings.color_mode = 'RGBA' if self.transparent_bg else 'RGB'


# ------------------------------------------------ pipeline B callbacks -----

def _dir_vec(angle_deg):
    a = math.radians(angle_deg)
    return (math.cos(a), math.sin(a), 0.0)


def u_iri_film(self, ctx):
    n = inodes()
    if not n:
        return
    n['ThickBase'].inputs[1].default_value = self.film_base
    n['HeightGain'].inputs[1].default_value = self.film_relief
    n['SweepAmp'].inputs[1].default_value = self.sweep_amount
    n['SweepFreq'].inputs[1].default_value = self.sweep_freq
    n['SweepDot'].inputs[1].default_value = _dir_vec(self.streak_angle)
    g = self.film_strength
    n['FilmGain'].inputs[7].default_value = (g, g, g, 1.0)


def u_iri_cover(self, ctx):
    n = inodes()
    if not n:
        return
    n['CoverFreq'].inputs[1].default_value = self.cover_freq
    n['CoverDot'].inputs[1].default_value = _dir_vec(self.streak_angle)
    # width 0 = a hairline streak, 1 = the film covers everything
    n['CoverRange'].inputs['From Min'].default_value = 1.0 - 2.0 * self.cover_width
    n['CoverStrength'].inputs[1].default_value = self.cover_opacity


def u_iri_substrate(self, ctx):
    n = inodes()
    if not n:
        return
    n['SubstrateColor'].outputs[0].default_value = tuple(self.substrate_color) + (1.0,)
    half = self.paper_grain * 0.5
    n['GrainRange'].inputs['To Min'].default_value = 1.0 - half
    n['GrainRange'].inputs['To Max'].default_value = 1.0 + half
    n['PaperFibre'].inputs['Scale'].default_value = self.paper_scale
    n['PaperMap'].inputs['Scale'].default_value = (1.0, max(0.01, self.paper_stretch), 1.0)
    n['GrainMix'].inputs['Factor'].default_value = self.paper_tooth


def u_iri_surface(self, ctx):
    n = inodes()
    if not n:
        return
    n['IriBump'].inputs['Strength'].default_value = self.iri_bump
    n['IriShadeMix'].inputs['Fac'].default_value = 0.0 if self.iri_flat else 1.0
    n['IriEmission'].inputs['Strength'].default_value = self.iri_glow
    n['IriPBSDF'].inputs['Roughness'].default_value = self.iri_roughness


def u_iri_map(self, ctx):
    n = inodes()
    if not n:
        return
    n['SimMap'].inputs['Scale'].default_value = (self.sim_tiling, self.sim_tiling, 1.0)
    n['BaseMap'].inputs['Rotation'].default_value[2] = math.radians(self.iri_rotation)


def u_aura(self, ctx):
    n = anodes()
    if not n:
        return
    # No aspect correction here. Generated coords are already isotropic under
    # the ortho camera; scaling X by the frame aspect squeezes features
    # horizontally and is what produced the vertical streaking.
    b = n['AuraMap']
    b.inputs['Scale'].default_value = (self.aura_scale, self.aura_scale, 1.0)
    b.inputs['Rotation'].default_value[2] = math.radians(self.aura_rotation)
    rs = random.Random(self.aura_seed)
    o = n['SeedOffset']
    o.inputs['X'].default_value = rs.uniform(-40.0, 40.0)
    o.inputs['Y'].default_value = rs.uniform(-40.0, 40.0)
    o.inputs['Z'].default_value = rs.uniform(-40.0, 40.0)
    n['WarpNoise1'].inputs['Scale'].default_value = self.aura_warp_scale
    n['WarpNoise2'].inputs['Scale'].default_value = self.aura_warp_scale * 1.9
    n['WarpAmt1'].inputs['Scale'].default_value = self.aura_warp
    n['WarpAmt2'].inputs['Scale'].default_value = self.aura_warp * 0.5
    f = n['FieldNoise']
    f.inputs['Scale'].default_value = self.aura_field_scale
    f.inputs['Detail'].default_value = self.aura_detail
    f.inputs['Roughness'].default_value = self.aura_roughness
    sh = n['FieldShape']
    if self.aura_steps > 1:
        # In zone mode the equaliser owns the distribution. Leaving the contrast
        # clamp in would create atoms at 0 and 1 that no curve can redistribute,
        # which is what made outer zones vanish.
        sh.inputs['From Min'].default_value = 0.0
        sh.inputs['From Max'].default_value = 1.0
    else:
        edge = 0.5 - 0.5 / max(1e-3, self.aura_contrast)
        sh.inputs['From Min'].default_value = edge
        sh.inputs['From Max'].default_value = 1.0 - edge
    # Zone mode: quantise the field into exactly as many levels as there are
    # palette stops and look them up with CONSTANT interpolation, so each zone
    # takes a palette colour verbatim. Interpolating first and posterising
    # afterwards is what produced the grey-mauve dead zones between hues.
    steps = n['FieldSteps']
    ramp = n['GradientRamp']
    # In zone mode every zone must land on a literal palette colour. If the ramp
    # carries more stops than there are brand colours, OKLab fills the gaps with
    # blended intermediates - and those blends are exactly the muddy mid-hues
    # that zone mode exists to eliminate. So keep the counts locked together.
    if self.aura_steps > 1 and self.palette_mode == 'BRAND' \
            and self.color_stops != self.aura_steps:
        global _MUTE
        prev, _MUTE = _MUTE, True
        try:
            self.color_stops = self.aura_steps
        finally:
            _MUTE = prev
        u_colors(self, ctx)

    if self.aura_steps > 1:
        steps.interpolation_type = 'STEPPED'
        steps.inputs['Steps'].default_value = float(self.aura_steps - 1)
        # nudge each level just inside its zone instead of exactly on the
        # boundary, where float rounding could drop it into the zone below
        eps = 0.25 / self.aura_steps
        steps.inputs['To Min'].default_value = eps
        steps.inputs['To Max'].default_value = 1.0 + eps
        ramp.color_ramp.interpolation = 'CONSTANT'
    else:
        steps.interpolation_type = 'LINEAR'
        steps.inputs['To Min'].default_value = 0.0
        steps.inputs['To Max'].default_value = 1.0
        ramp.color_ramp.interpolation = self.gradient_interp
    n['AuraEmission'].inputs['Strength'].default_value = self.aura_strength
    for i, r in enumerate((self.aura_drift, self.aura_drift * 0.68, self.aura_drift * 0.41), 1):
        n['OrbCosR%d' % i].inputs[1].default_value = r
        n['OrbSinR%d' % i].inputs[1].default_value = r


def sync_aura(ctx):
    if bpy.data.materials.get(AURA_MAT):
        u_aura(ctx.scene.wavetex, ctx)
        u_colors(ctx.scene.wavetex, ctx)
        u_interp(ctx.scene.wavetex, ctx)


def u_pipeline(self, ctx):
    if self.pipeline == 'IRIDESCENT' and bpy.data.materials.get(IRI_MAT) is None:
        return                      # nothing built yet; the Build button does it
    assign_pipeline(ctx)


def sync_iri(ctx):
    p = ctx.scene.wavetex
    if inodes() is None:
        return
    for fn in (u_iri_film, u_iri_cover, u_iri_substrate, u_iri_surface, u_iri_map):
        fn(p, ctx)
    n = inodes()
    ad = bpy.data.materials[IRI_MAT].node_tree.animation_data
    if ad:
        for fc in ad.drivers:
            fc.driver.expression = "frame / %d * %s" % (p.loop_frames, TAU)
    n['SimHeight'].image = bpy.data.images.get("WT_SimHeight")
    n['FilmLUT'].image = bpy.data.images.get("WT_FilmLUT")
    n['SimHeight'].image_user.frame_duration = p.loop_frames


def sync_fx(ctx):
    p = ctx.scene.wavetex
    for fn in (u_fx_lens, u_fx_blur, u_fx_bloom, u_fx_dither, u_fx_grain,
               u_fx_vignette, u_fx_exposure, u_fx_tone, u_fx_scrim, u_transparent):
        fn(p, ctx)


def sync_all(ctx):
    p = ctx.scene.wavetex
    if bpy.data.materials.get(MAT_NAME):
        for fn in (u_style, u_wave, u_colors, u_interp, u_facshape, u_noise,
                   u_overlay, u_surface, u_shade, u_filter, u_transform, u_view):
            fn(p, ctx)
    u_loop(p, ctx)
    sync_aura(ctx)
    sync_iri(ctx)
    sync_fx(ctx)


# A bulk apply (preset, library load) sets 60+ properties in a row. Letting
# every update callback fire would rebuild the palette and re-bake the scrim
# images once per property, so they are muted for the duration and one full
# sync runs at the end instead.
_MUTE = False


def _mute_wrap(fn):
    def inner(self, ctx):
        if _MUTE:
            return
        return fn(self, ctx)
    inner.__name__ = fn.__name__
    return inner


for _n, _f in list(globals().items()):
    if _n.startswith('u_') and callable(_f):
        globals()[_n] = _mute_wrap(_f)
del _n, _f


# ------------------------------------------------------------ properties ----

class WaveTexProps(bpy.types.PropertyGroup):
    wave_style: bpy.props.EnumProperty(
        name="Wave Style", update=u_style,
        items=[('WATER', "Water Wave", "Flowing diagonal bands, like a water surface"),
               ('RIPPLE', "Texture Ripple", "Concentric rings radiating from the centre")])
    wave_scale: bpy.props.FloatProperty(name="Scale", default=4.0, min=0.1, max=30.0, update=u_wave)
    distortion: bpy.props.FloatProperty(name="Distortion", default=6.0, min=0.0, max=30.0, update=u_wave)
    detail: bpy.props.FloatProperty(name="Detail", default=2.0, min=0.0, max=15.0, update=u_wave)
    speed: bpy.props.IntProperty(name="Speed (cycles/loop)", default=1, min=1, max=8, update=u_wave,
                                 description="Whole numbers only - keeps the loop seamless")

    rotation: bpy.props.FloatProperty(name="Rotation", default=0.0, min=-180, max=180, update=u_transform)
    offset_x: bpy.props.FloatProperty(name="Offset X", default=0.0, min=-2, max=2, update=u_transform)
    offset_y: bpy.props.FloatProperty(name="Offset Y", default=0.0, min=-2, max=2, update=u_transform)
    stretch_x: bpy.props.FloatProperty(name="Stretch X", default=1.0, min=0.05, max=5.0, update=u_transform)
    stretch_y: bpy.props.FloatProperty(name="Stretch Y", default=1.0, min=0.05, max=5.0, update=u_transform)

    palette_mode: bpy.props.EnumProperty(
        name="Palette", default='SEED', update=u_colors,
        items=[('SEED', "Generated", "Seed + colour harmony"),
               ('BRAND', "Brand Colours", "Blend your own colours through OKLab")])
    brand_count: bpy.props.IntProperty(name="Brand Colours", default=3, min=2, max=4, update=u_colors)
    brand_1: bpy.props.FloatVectorProperty(name="Colour 1", subtype='COLOR', size=3,
                                           default=(0.05, 0.07, 0.20), min=0.0, max=1.0, update=u_colors)
    brand_2: bpy.props.FloatVectorProperty(name="Colour 2", subtype='COLOR', size=3,
                                           default=(0.22, 0.14, 0.55), min=0.0, max=1.0, update=u_colors)
    brand_3: bpy.props.FloatVectorProperty(name="Colour 3", subtype='COLOR', size=3,
                                           default=(0.55, 0.30, 0.70), min=0.0, max=1.0, update=u_colors)
    brand_4: bpy.props.FloatVectorProperty(name="Colour 4", subtype='COLOR', size=3,
                                           default=(0.95, 0.62, 0.55), min=0.0, max=1.0, update=u_colors)
    brand_lift: bpy.props.FloatProperty(
        name="Tonal Spread", default=0.0, min=-0.5, max=0.5, update=u_colors,
        description="Push the ramp darker at one end and lighter at the other")

    seed: bpy.props.IntProperty(name="Seed", default=42, min=0, update=u_colors)
    harmony: bpy.props.EnumProperty(
        name="Harmony", default='TRIADIC', update=u_colors,
        items=[('ANALOGOUS', "Analogous", "Neighbouring hues - calm"),
               ('COMPLEMENTARY', "Complementary", "Opposite hues - high contrast"),
               ('TRIADIC', "Triadic", "Three evenly spaced hues"),
               ('TETRADIC', "Tetradic", "Four evenly spaced hues"),
               ('SPLIT', "Split Complementary", "One hue plus two beside its opposite"),
               ('MONOCHROME', "Monochrome", "One hue, varying value")])
    saturation: bpy.props.FloatProperty(name="Saturation", default=0.9, min=0.0, max=1.0, update=u_colors)
    brightness: bpy.props.FloatProperty(name="Brightness", default=1.0, min=0.2, max=2.0, update=u_colors)
    color_stops: bpy.props.IntProperty(name="Color Stops", default=5, min=3, max=8, update=u_colors)
    gradient_interp: bpy.props.EnumProperty(
        name="Blend", default='EASE', update=u_interp,
        items=[('EASE', "Ease", ""), ('LINEAR', "Linear", ""),
               ('B_SPLINE', "B-Spline", ""), ('CONSTANT', "Hard Steps", "")])
    color_cycles: bpy.props.FloatProperty(name="Color Cycles", default=1.0, min=0.25, max=8.0,
                                          update=u_facshape,
                                          description="How many times the gradient repeats across the wave")
    band_sharpness: bpy.props.FloatProperty(name="Band Sharpness", default=1.0, min=0.2, max=4.0,
                                            update=u_facshape,
                                            description="Biases the wave toward its dark or light bands")

    noise_amount: bpy.props.FloatProperty(name="Noise Amount", default=0.15, min=0.0, max=1.0, update=u_noise)
    noise_scale: bpy.props.FloatProperty(name="Noise Scale", default=6.0, min=0.1, max=40.0, update=u_noise)
    noise_detail: bpy.props.FloatProperty(name="Noise Detail", default=4.0, min=0.0, max=15.0, update=u_noise)
    noise_drift: bpy.props.FloatProperty(name="Noise Drift", default=0.5, min=0.0, max=3.0, update=u_noise,
                                         description="How far the noise travels over one loop")

    overlay_type: bpy.props.EnumProperty(
        name="Big Overlay", default='NONE', update=u_overlay,
        items=[('NONE', "None", ""), ('MAGIC', "Magic Swirl", ""), ('VORONOI', "Voronoi Cells", "")])
    overlay_amount: bpy.props.FloatProperty(name="Overlay Amount", default=0.4, min=0.0, max=1.0, update=u_overlay)
    overlay_scale: bpy.props.FloatProperty(name="Overlay Scale", default=1.5, min=0.1, max=10.0, update=u_overlay)

    bump_strength: bpy.props.FloatProperty(name="Normal Strength", default=0.3, min=0.0, max=2.0, update=u_surface)
    emission: bpy.props.FloatProperty(name="Glow", default=1.0, min=0.0, max=5.0, update=u_surface)
    roughness: bpy.props.FloatProperty(name="Roughness", default=0.4, min=0.0, max=1.0, update=u_surface)
    metallic: bpy.props.FloatProperty(name="Metallic", default=0.0, min=0.0, max=1.0, update=u_surface)
    shading_mode: bpy.props.EnumProperty(
        name="Render Mode", default='FLAT', update=u_shade,
        items=[('FLAT', "Flat / Unlit", "Exact gradient colors - use this for texture export"),
               ('SHADED', "Lit + Normals", "Principled shading so the bump/normal reads")])

    hue_shift: bpy.props.FloatProperty(name="Hue Shift", default=0.0, min=-0.5, max=0.5, update=u_filter)
    filter_sat: bpy.props.FloatProperty(name="Filter Saturation", default=1.0, min=0.0, max=2.0, update=u_filter)
    filter_value: bpy.props.FloatProperty(name="Filter Value", default=1.0, min=0.0, max=2.0, update=u_filter)
    contrast: bpy.props.FloatProperty(name="Contrast", default=0.0, min=-1.0, max=2.0, update=u_filter)
    filter_bright: bpy.props.FloatProperty(name="Brightness+", default=0.0, min=-0.5, max=0.5, update=u_filter)
    tint_amount: bpy.props.FloatProperty(name="Tint Amount", default=0.0, min=0.0, max=1.0, update=u_filter)
    tint_color: bpy.props.FloatVectorProperty(name="Tint Color", subtype='COLOR', size=3,
                                              default=(1.0, 0.4, 0.8), min=0.0, max=1.0, update=u_filter)

    # ---- pipeline B: simulated thin-film iridescence ----
    pipeline: bpy.props.EnumProperty(
        name="Pipeline", default='WAVE', update=u_pipeline,
        items=[('AURA', "Aura Flow", "Domain-warped noise - large soft organic blobs"),
               ('WAVE', "Wave Gradient", "Procedural animated gradient wave - directional bands"),
               ('IRIDESCENT', "Iridescent Film", "Simulated waves + real thin-film interference")])

    # ---- Aura Flow ----
    aura_scale: bpy.props.FloatProperty(
        name="Zoom", default=1.0, min=0.15, max=6.0, update=u_aura,
        description="Below 1 magnifies the field - fewer, bigger blobs")
    aura_rotation: bpy.props.FloatProperty(name="Rotation", default=0.0, min=-180.0, max=180.0,
                                           update=u_aura)
    aura_field_scale: bpy.props.FloatProperty(
        name="Blob Size", default=1.1, min=0.2, max=6.0, update=u_aura,
        description="Feature frequency. ~1.0 gives blobs about a quarter of the frame")
    aura_warp: bpy.props.FloatProperty(
        name="Warp", default=0.55, min=0.0, max=2.0, update=u_aura,
        description="How far the noise bends its own sample coordinates. 0 is plain fBm")
    aura_warp_scale: bpy.props.FloatProperty(name="Warp Detail", default=1.6, min=0.2, max=8.0,
                                             update=u_aura)
    aura_detail: bpy.props.FloatProperty(name="Detail", default=3.0, min=0.0, max=12.0, update=u_aura)
    aura_roughness: bpy.props.FloatProperty(name="Roughness", default=0.45, min=0.0, max=1.0,
                                            update=u_aura)
    aura_contrast: bpy.props.FloatProperty(
        name="Field Contrast", default=2.5, min=1.0, max=12.0, update=u_aura,
        description="Higher pushes the field to its extremes, giving distinct colour territories")
    aura_ground_weight: bpy.props.FloatProperty(
        name="Ground Share", default=0.0, min=0.0, max=0.85, update=u_aura,
        description="Fraction of the frame the first palette colour should own. "
                    "Both reference styles use a dominant ground - near-black chassis "
                    "or near-white paper - with smaller colour cores. Press Balance "
                    "Zones after changing it")
    aura_seed: bpy.props.IntProperty(
        name="Composition", default=0, min=0, max=9999, update=u_aura,
        description="Samples the field at a different origin. Changes where hues pool "
                    "without changing feature size - step it to escape a stale composition")
    aura_steps: bpy.props.IntProperty(
        name="Colour Zones", default=0, min=0, max=12, update=u_aura,
        description="0 blends smoothly. 3-5 quantises the field into flat colour zones "
                    "taken straight from the palette; soften the edges with a little blur")
    aura_strength: bpy.props.FloatProperty(name="Strength", default=1.0, min=0.0, max=4.0,
                                           update=u_aura)
    aura_drift: bpy.props.FloatProperty(
        name="Drift", default=0.22, min=0.0, max=1.0, update=u_aura,
        description="Orbit radius of the animation. Small values read as slow and heavy")

    sim_res: bpy.props.EnumProperty(
        name="Sim Resolution", default='256',
        items=[('128', "128", "Fast"), ('256', "256", "Balanced"), ('512', "512", "Sharp, slower bake")])
    sim_domain: bpy.props.FloatProperty(
        name="Tile Size (m)", default=0.35, min=0.02, max=5.0,
        description="Physical size of the simulated tile. Small = tight capillary ripples")
    sim_wind: bpy.props.FloatProperty(name="Wind Speed", default=2.4, min=0.2, max=20.0)
    sim_wind_angle: bpy.props.FloatProperty(name="Wind Angle", default=24.0, min=-180.0, max=180.0)
    sim_capillary: bpy.props.FloatProperty(
        name="Surface Tension", default=1.0, min=0.0, max=6.0,
        description="Scales the capillary term. 0 gives pure gravity waves (slow swell)")
    sim_duration: bpy.props.FloatProperty(
        name="Sim Seconds", default=4.0, min=0.5, max=30.0,
        description="Seconds of physical time the loop spans")
    sim_seed: bpy.props.IntProperty(name="Sim Seed", default=7, min=0)
    sim_tiling: bpy.props.FloatProperty(name="Sim Tiling", default=1.0, min=0.1, max=8.0, update=u_iri_map)
    iri_rotation: bpy.props.FloatProperty(name="Rotation", default=0.0, min=-180, max=180, update=u_iri_map)

    film_ior: bpy.props.FloatProperty(
        name="Film IOR", default=1.35, min=1.01, max=3.0,
        description="Refractive index of the film. 1.33 soap, 1.45 oil, 2.0+ metallic foil")
    substrate_ior: bpy.props.FloatProperty(
        name="Substrate IOR", default=1.0, min=1.0, max=4.0,
        description="1.0 = free-standing film (vivid). Higher = film on glass/plastic")
    film_angle: bpy.props.FloatProperty(name="View Angle", default=0.0, min=0.0, max=80.0)
    film_thickness_max: bpy.props.FloatProperty(
        name="Max Thickness (nm)", default=1400.0, min=200.0, max=4000.0,
        description="Thickness the LUT spans. Larger = more colour cycles, more washed out")

    film_base: bpy.props.FloatProperty(name="Thickness Bias", default=0.35, min=0.0, max=1.0, update=u_iri_film)
    film_relief: bpy.props.FloatProperty(
        name="Sim Influence", default=0.30, min=0.0, max=1.0, update=u_iri_film,
        description="How strongly the simulated waves modulate film thickness")
    sweep_amount: bpy.props.FloatProperty(name="Sweep Amount", default=0.38, min=0.0, max=1.0, update=u_iri_film)
    sweep_freq: bpy.props.FloatProperty(
        name="Colour Cycles", default=3.0, min=0.2, max=20.0, update=u_iri_film,
        description="How many interference cycles cross the streak")
    film_strength: bpy.props.FloatProperty(name="Film Brightness", default=0.95, min=0.0, max=2.0,
                                           update=u_iri_film)

    streak_angle: bpy.props.FloatProperty(name="Streak Angle", default=35.0, min=-180, max=180,
                                          update=u_iri_cover)
    cover_freq: bpy.props.FloatProperty(name="Streak Repeat", default=0.7, min=0.1, max=8.0,
                                        update=u_iri_cover)
    cover_width: bpy.props.FloatProperty(
        name="Streak Width", default=0.42, min=0.01, max=1.0, update=u_iri_cover,
        description="1.0 covers the whole surface, small values give a narrow streak")
    cover_opacity: bpy.props.FloatProperty(name="Streak Opacity", default=1.0, min=0.0, max=1.0,
                                           update=u_iri_cover)

    substrate_color: bpy.props.FloatVectorProperty(
        name="Substrate", subtype='COLOR', size=3, default=(0.045, 0.20, 0.38),
        min=0.0, max=1.0, update=u_iri_substrate)
    paper_grain: bpy.props.FloatProperty(name="Paper Grain", default=0.36, min=0.0, max=1.5,
                                         update=u_iri_substrate)
    paper_scale: bpy.props.FloatProperty(name="Fibre Scale", default=90.0, min=5.0, max=600.0,
                                         update=u_iri_substrate)
    paper_stretch: bpy.props.FloatProperty(name="Fibre Stretch", default=0.18, min=0.01, max=1.0,
                                           update=u_iri_substrate)
    paper_tooth: bpy.props.FloatProperty(name="Tooth Mix", default=0.5, min=0.0, max=1.0,
                                         update=u_iri_substrate)

    iri_bump: bpy.props.FloatProperty(name="Relief", default=0.25, min=0.0, max=2.0, update=u_iri_surface)
    iri_glow: bpy.props.FloatProperty(name="Glow", default=1.0, min=0.0, max=4.0, update=u_iri_surface)
    iri_roughness: bpy.props.FloatProperty(name="Roughness", default=0.35, min=0.0, max=1.0,
                                           update=u_iri_surface)
    iri_flat: bpy.props.BoolProperty(name="Flat / Unlit", default=True, update=u_iri_surface)

    # ---- global post effects (compositor) ----
    use_blur: bpy.props.BoolProperty(name="Enable Blur", default=True, update=u_fx_blur)
    use_bloom: bpy.props.BoolProperty(name="Enable Bloom", default=True, update=u_fx_bloom)
    use_dither: bpy.props.BoolProperty(name="Enable Dither", default=True, update=u_fx_dither)
    use_grain: bpy.props.BoolProperty(name="Enable Grain", default=True, update=u_fx_grain)
    use_lens: bpy.props.BoolProperty(name="Enable Lens", default=True, update=u_fx_lens)
    use_vignette: bpy.props.BoolProperty(name="Enable Vignette", default=True, update=u_fx_vignette)
    use_scrim: bpy.props.BoolProperty(name="Enable Scrim", default=True, update=u_fx_scrim)
    use_tone: bpy.props.BoolProperty(name="Enable Tone Range", default=True, update=u_fx_tone)
    blur_amount: bpy.props.FloatProperty(
        name="Blur", default=0.0, min=0.0, max=100.0, update=u_fx_blur,
        description="Percent-of-image gaussian blur. Large values turn the wave into a soft mesh gradient")
    painterly: bpy.props.FloatProperty(
        name="Painterly", default=0.0, min=0.0, max=30.0, update=u_fx_blur,
        description="Anisotropic Kuwahara - flattens into smooth painted regions. 0 disables it")
    pixelate: bpy.props.IntProperty(
        name="Pixelate", default=1, min=1, max=64, update=u_fx_blur,
        description="Chunky pixel blocks. 1 disables it")

    bloom: bpy.props.FloatProperty(name="Bloom", default=0.0, min=0.0, max=1.0, update=u_fx_bloom)
    bloom_threshold: bpy.props.FloatProperty(name="Bloom Threshold", default=0.45, min=0.0, max=2.0,
                                             update=u_fx_bloom)
    bloom_size: bpy.props.FloatProperty(name="Bloom Size", default=7.0, min=1.0, max=9.0, update=u_fx_bloom)

    posterize_steps: bpy.props.IntProperty(
        name="Posterize Steps", default=0, min=0, max=64, update=u_fx_dither,
        description="Quantise colors to N steps. 0 disables it")
    dither_mode: bpy.props.EnumProperty(
        name="Dither", default='ORDERED', update=u_fx_dither,
        items=[('NONE', "Off", ""),
               ('ORDERED', "Ordered (Bayer)", "Crosshatch pattern - the classic retro dither"),
               ('NOISE', "Noise", "Random dither - smoother, more filmic")])
    dither_amount: bpy.props.FloatProperty(
        name="Dither Amount", default=1.0, min=0.0, max=2.0, update=u_fx_dither,
        description="1.0 spans exactly one posterize step, which is what removes banding")

    grain: bpy.props.FloatProperty(
        name="Grain", default=0.0, min=0.0, max=1.0, update=u_fx_grain,
        description="Measured on real grainy-gradient plates: ~0.06-0.12 is a normal "
                    "grainy look, 0.16-0.20 is the heavy 'chaotic' end")
    grain_rolloff: bpy.props.FloatProperty(
        name="Highlight Rolloff", default=1.0, min=0.0, max=1.0, update=u_fx_grain,
        description="How much the grain fades out of bright areas. 0 is flat digital "
                    "noise everywhere; 1 matches a real scan")
    grain_animate: bpy.props.BoolProperty(
        name="Animate Grain", default=True, update=u_fx_grain,
        description="Re-roll the grain every frame, the way real film does. Off holds a "
                    "single still field, which is what you want for a static plate")
    grain_size: bpy.props.FloatProperty(
        name="Grain Size", default=0.85, min=0.0, max=8.0, update=u_grain_build,
        description="Crystal size in pixels. Neighbouring pixels are correlated over "
                    "this radius, which is what separates film grain from digital noise")
    grain_roughness: bpy.props.FloatProperty(
        name="Grain Roughness", default=0.5, min=0.0, max=1.0, update=u_grain_build,
        description="Low keeps the clumps creamy and soft-edged; high makes individual "
                    "crystals read as distinct specks")
    grain_scale: bpy.props.FloatProperty(
        name="Grain Scale", default=1.0, min=0.25, max=6.0, update=u_grain_build,
        description="Enlargement factor. A smaller negative is blown up more to fill the "
                    "same frame, which is exactly why 16mm looks grainier than 35mm")
    grain_chroma: bpy.props.FloatProperty(
        name="Colour Grain", default=0.30, min=0.0, max=1.0, update=u_grain_build,
        description="How much of the grain is chromatic. Real colour negative sits low - "
                    "grain is mostly a luminance fluctuation. Push it high and every pixel "
                    "takes a random hue, which reads as rainbow sensor static, not film. "
                    "0 is pure luminance grain, like black and white stock")

    chromatic: bpy.props.FloatProperty(name="Chromatic Aberration", default=0.0, min=0.0, max=0.3,
                                       update=u_fx_lens)
    lens_distort: bpy.props.FloatProperty(name="Lens Distort", default=0.0, min=-0.5, max=0.5,
                                          update=u_fx_lens)

    vignette: bpy.props.FloatProperty(name="Vignette", default=0.0, min=0.0, max=1.0, update=u_fx_vignette)
    vignette_size: bpy.props.FloatProperty(name="Vignette Size", default=0.85, min=0.1, max=1.5,
                                           update=u_fx_vignette)
    vignette_softness: bpy.props.FloatProperty(name="Vignette Softness", default=25.0, min=0.0, max=60.0,
                                               update=u_fx_vignette)

    exposure: bpy.props.FloatProperty(name="Exposure", default=0.0, min=-4.0, max=4.0, update=u_fx_exposure)

    # ---- legibility / layout tools ----
    tone_floor: bpy.props.FloatProperty(
        name="Tone Floor", default=0.0, min=0.0, max=1.0, update=u_fx_tone,
        description="Lift the darkest value. Narrowing the tone range is what keeps "
                    "a background from competing with text")
    tone_ceiling: bpy.props.FloatProperty(name="Tone Ceiling", default=1.0, min=0.0, max=1.0,
                                          update=u_fx_tone)
    scrim_strength: bpy.props.FloatProperty(name="Scrim", default=0.0, min=0.0, max=1.0, update=u_fx_scrim)
    scrim_color: bpy.props.FloatVectorProperty(name="Scrim Colour", subtype='COLOR', size=3,
                                               default=(0.0, 0.0, 0.0), min=0.0, max=1.0, update=u_fx_scrim)
    scrim_dir: bpy.props.EnumProperty(
        name="Scrim From", default='BOTTOM', update=u_fx_scrim,
        items=[('BOTTOM', "Bottom", ""), ('TOP', "Top", ""), ('LEFT', "Left", ""),
               ('RIGHT', "Right", ""), ('RADIAL', "Edges", ""), ('FULL', "Whole Frame", "")])
    scrim_coverage: bpy.props.FloatProperty(name="Coverage", default=0.55, min=0.0, max=1.0,
                                            update=u_fx_scrim)
    scrim_softness: bpy.props.FloatProperty(name="Softness", default=0.45, min=0.01, max=1.5,
                                            update=u_fx_scrim)
    edge_fade: bpy.props.BoolProperty(
        name="Edge Fade to Alpha", default=False, update=u_fx_scrim,
        description="Fade the plate out at the frame edge so it can sit over a page")
    edge_inset: bpy.props.FloatProperty(name="Fade Inset", default=0.0, min=0.0, max=0.45,
                                        update=u_fx_scrim)
    edge_softness: bpy.props.FloatProperty(name="Fade Softness", default=0.15, min=0.01, max=0.5,
                                           update=u_fx_scrim)
    text_color: bpy.props.FloatVectorProperty(name="Text Colour", subtype='COLOR', size=3,
                                              default=(1.0, 1.0, 1.0), min=0.0, max=1.0)
    text_size_class: bpy.props.EnumProperty(
        name="Text", default='BODY',
        items=[('BODY', "Body text", "Needs 4.5:1 for AA"),
               ('LARGE', "Large / heading", "18pt+ or 14pt bold, needs 3:1 for AA")])
    contrast_report: bpy.props.StringProperty(name="Contrast Report", default="")

    viewport_fx: bpy.props.EnumProperty(
        name="Live FX", default='DISABLED', update=u_viewport_fx,
        items=[('DISABLED', "Off", "Do not preview effects in the viewport (safest)"),
               ('CAMERA', "In Camera", "Preview effects inside the camera frame. This runs "
                                       "the whole compositor live in EEVEE alongside your "
                                       "render - if Blender becomes unstable, turn it off"),
               ('ALWAYS', "Always", "Preview effects everywhere - heaviest option")])
    transparent_bg: bpy.props.BoolProperty(
        name="Transparent Background", default=False, update=u_transparent,
        description="Render with alpha so the texture can sit over a web page")
    export_format: bpy.props.EnumProperty(
        name="Format", default='PNG',
        items=[('PNG', "PNG Sequence", "Numbered frames - highest quality, largest"),
               ('MP4', "MP4 (H.264)", "Small file, no alpha - best for a full-bleed background"),
               ('WEBM', "WebM (VP9 + alpha)", "Web friendly and keeps transparency")])

    loop_frames: bpy.props.IntProperty(name="Loop Length (frames)", default=120, min=24, max=600, update=u_loop)
    loop_seconds: bpy.props.FloatProperty(
        name="Loop Length (seconds)", default=4.0, min=1.0, max=20.0, update=u_loop_seconds,
        description="Designers think in seconds. Sets the frame count from the scene frame rate")
    poster_frame: bpy.props.IntProperty(
        name="Poster Frame", default=1, min=1, max=600,
        description="Which frame to use as the static fallback image")
    view_transform: bpy.props.EnumProperty(
        name="View Transform", default='Standard', update=u_view,
        items=[('Standard', "Standard (true colors)", "Exact texture colors - use for export"),
               ('AgX', "AgX (filmic)", "Softer, cinematic response"),
               ('Filmic', "Filmic", "Legacy filmic response")])


# --------------------------------------------------------------- presets ----

PRESETS = {
    'OCEAN': dict(wave_style='WATER', wave_scale=4.0, distortion=6.0, detail=2.0, speed=1,
                  harmony='ANALOGOUS', seed=12, saturation=0.8, brightness=1.0, color_stops=5,
                  color_cycles=1.0, band_sharpness=1.0, noise_amount=0.18, noise_scale=5.0,
                  overlay_type='NONE', bump_strength=0.4, emission=1.0, roughness=0.25,
                  gradient_interp='EASE'),
    'LAVA': dict(wave_style='WATER', wave_scale=3.0, distortion=12.0, detail=4.0, speed=1,
                 harmony='COMPLEMENTARY', seed=7, saturation=1.0, brightness=1.3, color_stops=5,
                 color_cycles=1.0, band_sharpness=1.4, noise_amount=0.3, noise_scale=8.0,
                 overlay_type='VORONOI', overlay_amount=0.35, bump_strength=0.8, emission=1.3,
                 roughness=0.6, gradient_interp='EASE'),
    'RIPPLE': dict(wave_style='RIPPLE', wave_scale=5.0, distortion=2.0, detail=1.0, speed=2,
                   harmony='TRIADIC', seed=99, saturation=0.9, brightness=1.1, color_stops=6,
                   color_cycles=2.0, band_sharpness=1.0, noise_amount=0.05, noise_scale=10.0,
                   overlay_type='NONE', bump_strength=0.6, emission=1.0, roughness=0.3,
                   gradient_interp='EASE'),
    'RETRO': dict(wave_style='WATER', wave_scale=2.0, distortion=3.0, detail=0.0, speed=1,
                  harmony='TETRADIC', seed=5, saturation=1.0, brightness=1.2, color_stops=6,
                  color_cycles=1.0, band_sharpness=1.0, noise_amount=0.0, noise_scale=4.0,
                  overlay_type='NONE', bump_strength=0.0, emission=1.0, roughness=0.5,
                  gradient_interp='CONSTANT'),
    'PLASMA': dict(wave_style='RIPPLE', wave_scale=3.0, distortion=18.0, detail=6.0, speed=1,
                   harmony='SPLIT', seed=314, saturation=0.95, brightness=1.2, color_stops=7,
                   color_cycles=1.5, band_sharpness=1.0, noise_amount=0.45, noise_scale=3.0,
                   overlay_type='MAGIC', overlay_amount=0.5, bump_strength=0.5, emission=1.0,
                   roughness=0.4, gradient_interp='EASE'),
    'MONOSILK': dict(wave_style='WATER', wave_scale=5.0, distortion=8.0, detail=3.0, speed=1,
                     harmony='MONOCHROME', seed=77, saturation=0.7, brightness=1.0, color_stops=5,
                     color_cycles=1.0, band_sharpness=1.0, noise_amount=0.12, noise_scale=7.0,
                     overlay_type='NONE', bump_strength=0.9, emission=1.0, roughness=0.15,
                     metallic=0.7, gradient_interp='EASE', shading_mode='SHADED'),
}


# ------------------------------------------------------------- operators ----

class WT_OT_setup(bpy.types.Operator):
    bl_idname = "wavetex.setup"
    bl_label = "Build / Rebuild Texture Stage"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        build_stage(ctx)
        r = ctx.scene.render
        build_patterns(r.resolution_x, r.resolution_y)
        build_compositor(ctx.scene)
        sync_all(ctx)
        for area in ctx.screen.areas:
            if area.type == 'VIEW_3D':
                for sp in area.spaces:
                    if sp.type == 'VIEW_3D':
                        sp.shading.type = 'MATERIAL'
                        sp.shading.use_scene_world = False
                        sp.shading.studiolight_background_alpha = 0.0
                        sp.shading.use_compositor = ctx.scene.wavetex.viewport_fx
                        sp.region_3d.view_perspective = 'CAMERA'
        self.report({'INFO'}, "Wave texture stage ready")
        return {'FINISHED'}


class WT_OT_sync(bpy.types.Operator):
    bl_idname = "wavetex.sync"
    bl_label = "Sync Panel to Material"

    def execute(self, ctx):
        sync_all(ctx)
        return {'FINISHED'}


class WT_OT_random_seed(bpy.types.Operator):
    bl_idname = "wavetex.random_seed"
    bl_label = "Randomize Seed"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        ctx.scene.wavetex.seed = random.randint(0, 99999)
        return {'FINISHED'}


class WT_OT_seed_step(bpy.types.Operator):
    bl_idname = "wavetex.seed_step"
    bl_label = "Step Seed"
    bl_options = {'REGISTER', 'UNDO'}
    delta: bpy.props.IntProperty(default=1)

    def execute(self, ctx):
        p = ctx.scene.wavetex
        p.seed = max(0, p.seed + self.delta)
        return {'FINISHED'}


class WT_OT_cycle_harmony(bpy.types.Operator):
    bl_idname = "wavetex.cycle_harmony"
    bl_label = "Next Harmony"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        p = ctx.scene.wavetex
        keys = [i[0] for i in p.bl_rna.properties['harmony'].enum_items]
        p.harmony = keys[(keys.index(p.harmony) + 1) % len(keys)]
        return {'FINISHED'}


class WT_OT_reverse_gradient(bpy.types.Operator):
    bl_idname = "wavetex.reverse_gradient"
    bl_label = "Flip Gradient"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        p = ctx.scene.wavetex
        cols = gen_palette(p.seed, p.harmony, p.saturation, p.brightness, p.color_stops)
        apply_palette(list(reversed(cols)))
        return {'FINISHED'}


class WT_OT_random_filter(bpy.types.Operator):
    bl_idname = "wavetex.random_filter"
    bl_label = "Randomize"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        p = ctx.scene.wavetex
        p.hue_shift = random.uniform(-0.5, 0.5)
        p.contrast = random.uniform(0.0, 0.8)
        p.tint_amount = random.choice([0.0, 0.0, random.uniform(0.15, 0.6)])
        p.tint_color = (random.random(), random.random(), random.random())
        return {'FINISHED'}


class WT_OT_reset_filter(bpy.types.Operator):
    bl_idname = "wavetex.reset_filter"
    bl_label = "Reset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        p = ctx.scene.wavetex
        p.hue_shift = 0.0
        p.filter_sat = 1.0
        p.filter_value = 1.0
        p.contrast = 0.0
        p.filter_bright = 0.0
        p.tint_amount = 0.0
        return {'FINISHED'}


class WT_OT_surprise(bpy.types.Operator):
    bl_idname = "wavetex.surprise"
    bl_label = "Surprise Me"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        p = ctx.scene.wavetex
        bpy.ops.wavetex.preset(name_id=random.choice(list(PRESETS)))
        p.seed = random.randint(0, 99999)
        keys = [i[0] for i in p.bl_rna.properties['harmony'].enum_items]
        p.harmony = random.choice(keys)
        p.color_cycles = random.choice([1.0, 1.0, 1.5, 2.0, 3.0])
        p.rotation = random.uniform(-180, 180)
        return {'FINISHED'}


class WT_OT_preset(bpy.types.Operator):
    bl_idname = "wavetex.preset"
    bl_label = "Apply Preset"
    bl_options = {'REGISTER', 'UNDO'}
    name_id: bpy.props.StringProperty()

    def execute(self, ctx):
        p = ctx.scene.wavetex
        for k, v in PRESETS[self.name_id].items():
            setattr(p, k, v)
        return {'FINISHED'}


class WT_OT_play_loop(bpy.types.Operator):
    bl_idname = "wavetex.play_loop"
    bl_label = "Play Loop"

    def execute(self, ctx):
        sc = ctx.scene
        sc.use_preview_range = False
        sc.frame_start = 1
        sc.frame_end = sc.wavetex.loop_frames
        bpy.ops.screen.animation_cancel(restore_frame=False)
        bpy.ops.screen.animation_play()
        return {'FINISHED'}


class WT_OT_stop(bpy.types.Operator):
    bl_idname = "wavetex.stop"
    bl_label = "Stop"

    def execute(self, ctx):
        bpy.ops.screen.animation_cancel(restore_frame=False)
        return {'FINISHED'}


class WT_OT_rewind(bpy.types.Operator):
    bl_idname = "wavetex.rewind"
    bl_label = "Rewind"

    def execute(self, ctx):
        ctx.scene.frame_set(ctx.scene.frame_start)
        return {'FINISHED'}


# ========================================================================
#  PRESET LIBRARY - serialisation, user saves, delete
# ========================================================================

_SKIP_SERIALISE = {'rna_type', 'name', 'view_transform', 'export_format',
                   'transparent_bg', 'viewport_fx'}


def props_to_dict(p):
    """Snapshot every tweakable setting. Output config (resolution, format) is
    deliberately excluded so applying a look never changes your export target."""
    d = {}
    for prop in p.bl_rna.properties:
        pid = prop.identifier
        if pid in _SKIP_SERIALISE or prop.is_readonly:
            continue
        try:
            v = getattr(p, pid)
        except Exception:
            continue
        if prop.type in {'FLOAT', 'INT', 'BOOLEAN'} and getattr(prop, 'is_array', False):
            d[pid] = list(v)
        elif prop.type in {'FLOAT', 'INT', 'BOOLEAN', 'STRING', 'ENUM'}:
            d[pid] = v
    # a hand-edited ramp is not reproducible from seed/harmony, so store it too
    mat = bpy.data.materials.get(MAT_NAME)
    if mat:
        ramp = mat.node_tree.nodes['GradientRamp'].color_ramp
        d['_ramp'] = [[e.position] + list(e.color) for e in ramp.elements]
        d['_ramp_interp'] = ramp.color_ramp.interpolation if hasattr(ramp, 'color_ramp') \
            else ramp.interpolation
    return d


# Settings that describe the workspace rather than the look. A preset must not
# reach in and change your output format or the text colour you are checking
# contrast against.
_KEEP_ON_RESET = _SKIP_SERIALISE | {'text_color', 'contrast_report',
                                    'loop_seconds', 'resolution_preset'}


def prop_defaults():
    """Factory values for every look-defining property."""
    out = {}
    for prop in WaveTexProps.bl_rna.properties:
        pid = prop.identifier
        if prop.is_readonly or pid in _KEEP_ON_RESET or pid == 'rna_type':
            continue
        try:
            if prop.type == 'ENUM':
                out[pid] = prop.default
            elif getattr(prop, 'is_array', False):
                out[pid] = list(prop.default_array)
            elif prop.type in {'FLOAT', 'INT', 'BOOLEAN', 'STRING'}:
                out[pid] = prop.default
        except Exception:
            pass
    return out


def dict_to_props(p, d, ctx):
    """Apply a look. Anything the preset does not mention is returned to its
    factory value first, so a preset always renders the same regardless of what
    the session was doing beforehand."""
    global _MUTE
    ramp_data = d.get('_ramp')
    merged = prop_defaults()
    merged.update({k: v for k, v in d.items()
                   if not k.startswith('_') and k not in _KEEP_ON_RESET})
    _MUTE = True
    try:
        for k, v in merged.items():
            if not hasattr(p, k):
                continue
            try:
                setattr(p, k, tuple(v) if isinstance(v, list) else v)
            except Exception:
                pass               # enum values can disappear between versions
    finally:
        _MUTE = False
    mat = bpy.data.materials.get(MAT_NAME)
    if ramp_data and mat:
        ramp = mat.node_tree.nodes['GradientRamp'].color_ramp
        while len(ramp.elements) > 1:
            ramp.elements.remove(ramp.elements[-1])
        ramp.elements[0].position = ramp_data[0][0]
        ramp.elements[0].color = ramp_data[0][1:]
        for row in ramp_data[1:]:
            ramp.elements.new(row[0]).color = row[1:]
        if d.get('_ramp_interp'):
            try:
                ramp.interpolation = d['_ramp_interp']
            except Exception:
                pass
    sync_all(ctx)


def library_path():
    d = bpy.utils.user_resource('CONFIG', path="wavetex", create=True)
    return os.path.join(d, "presets.json")


def library_load():
    path = library_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print("[wave_texture_maker] could not read preset library:", exc)
        return {}


def library_write(data):
    try:
        with open(library_path(), 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        return True
    except Exception as exc:
        print("[wave_texture_maker] could not write preset library:", exc)
        return False


def library_refresh(scene):
    """Mirror the on-disk library into the collection the UIList draws."""
    data = library_load()
    coll = scene.wavetex_library
    coll.clear()
    for name in sorted(data):
        it = coll.add()
        it.name = name
        it.pipeline = data[name].get('pipeline', 'WAVE')
    scene.wavetex_library_index = min(scene.wavetex_library_index, max(0, len(coll) - 1))


IRI_PRESETS = {
    'FOIL': dict(film_ior=1.35, substrate_ior=1.0, film_thickness_max=1400.0, film_base=0.35,
                 film_relief=0.30, sweep_amount=0.38, sweep_freq=3.0, film_strength=0.95,
                 streak_angle=35.0, cover_freq=0.7, cover_width=0.42, cover_opacity=1.0,
                 substrate_color=(0.045, 0.20, 0.38), paper_grain=0.36, iri_bump=0.25),
    'STREAK': dict(film_ior=1.35, substrate_ior=1.0, film_thickness_max=1400.0, film_base=0.30,
                   film_relief=0.18, sweep_amount=0.42, sweep_freq=6.0, film_strength=1.0,
                   streak_angle=35.0, cover_freq=1.1, cover_width=0.18, cover_opacity=1.0,
                   substrate_color=(0.045, 0.20, 0.38), paper_grain=0.40, iri_bump=0.15),
    'OIL': dict(film_ior=1.45, substrate_ior=1.33, film_thickness_max=1100.0, film_base=0.30,
                film_relief=0.50, sweep_amount=0.26, sweep_freq=2.4, film_strength=1.15,
                streak_angle=0.0, cover_freq=0.4, cover_width=1.0, cover_opacity=1.0,
                substrate_color=(0.015, 0.02, 0.035), paper_grain=0.05, iri_bump=0.45),
    'BUBBLE': dict(film_ior=1.33, substrate_ior=1.0, film_thickness_max=900.0, film_base=0.40,
                   film_relief=0.65, sweep_amount=0.15, sweep_freq=1.5, film_strength=1.0,
                   streak_angle=90.0, cover_freq=0.3, cover_width=1.0, cover_opacity=1.0,
                   substrate_color=(0.01, 0.01, 0.02), paper_grain=0.02, iri_bump=0.30),
    'PEARL': dict(film_ior=1.6, substrate_ior=1.5, film_thickness_max=3200.0, film_base=0.5,
                  film_relief=0.35, sweep_amount=0.25, sweep_freq=1.2, film_strength=0.7,
                  streak_angle=20.0, cover_freq=0.5, cover_width=0.8, cover_opacity=0.9,
                  substrate_color=(0.55, 0.52, 0.58), paper_grain=0.18, iri_bump=0.20),
}


def _lin(hexstr):
    """sRGB hex -> Blender linear, so preset colours match the source swatch."""
    out = []
    for i in (0, 2, 4):
        c = int(hexstr[i:i + 2], 16) / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return tuple(out)


# Shared spine for every curated look. The values that matter most:
#   * wave_scale stays under ~0.35 so one feature spans the frame. Measured on
#     real sites, background features are 2.4-5x viewport width; small features
#     are the single clearest tell of a generated background.
#   * loop_frames 480 = 16.0s at 30fps. Real brands run 14-24s ambient loops
#     (OpenAI 14/18/24s, claude.com 19s) and Resend authors its loops at
#     exactly 10.000s and 16.000s.
#   * dither carries anti-banding; decorative grain is separate and is dropped
#     on dark looks, where sRGB expands shadows and grain reads as noise.
_EX = dict(pipeline='WAVE', palette_mode='BRAND', shading_mode='FLAT', wave_style='WATER',
           speed=1, detail=1.0, overlay_type='NONE', posterize_steps=0, pixelate=1,
           gradient_interp='EASE', color_cycles=1.0, dither_mode='NOISE', dither_amount=1.0,
           bloom=0.0, vignette=0.0, tone_floor=0.0, tone_ceiling=1.0, chromatic=0.0,
           lens_distort=0.0, painterly=0.0, exposure=0.0, scrim_strength=0.0,
           edge_fade=False, brightness=1.0, saturation=1.0, brand_lift=0.0,
           noise_drift=0.35, loop_frames=480, stretch_y=1.0, offset_x=0.0, offset_y=0.0)

EXAMPLES = [
    # Every stop is kept light enough that near-black body text clears 4.5:1
    # across the whole sweep - a saturated mesh only stays usable if the darkest
    # stop is still a tint, not a mid-tone.
    ('SUNRISE', "Sunrise Mesh  (light, saturated)", dict(
        _EX, brand_count=4, brand_1=_lin("A8AEF5"), brand_2=_lin("F58FC6"),
        brand_3=_lin("FDAE6E"), brand_4=_lin("FFD45C"), color_stops=6, band_sharpness=1.0,
        wave_scale=0.34, distortion=6.5, detail=2.0, noise_amount=0.34, noise_scale=0.55,
        noise_detail=4.0, blur_amount=4.0, stretch_x=1.35, rotation=-30.0,
        grain=0.030, grain_animate=True)),

    ('IVORY', "Warm Paper  (light, near-flat)", dict(
        _EX, brand_count=4, brand_1=_lin("FAF9F5"), brand_2=_lin("F0EEE6"),
        brand_3=_lin("E3DACC"), brand_4=_lin("D4A27F"), color_stops=6, band_sharpness=1.8,
        wave_scale=0.22, distortion=4.5, noise_amount=0.40, noise_scale=0.55,
        noise_detail=3.0, blur_amount=8.0, stretch_x=1.5, rotation=16.0,
        grain=0.050, grain_animate=True)),

    ('NOCTURNE', "Nocturne  (dark, indigo bloom)", dict(
        _EX, brand_count=3, brand_1=_lin("08090A"), brand_2=_lin("14162B"),
        brand_3=_lin("5E6AD2"), color_stops=5, band_sharpness=1.5,
        wave_scale=0.26, distortion=5.0, noise_amount=0.42, noise_scale=0.50,
        noise_detail=3.0, blur_amount=9.0, stretch_x=1.5, rotation=-22.0,
        grain=0.0, grain_animate=False, dither_amount=1.4)),

    # brand_3 is deliberately dimmer than Clerk's #64E5FF cyan: at full
    # brightness the glow drops white body text under 4.5:1 across ~16% of the
    # frame. This version clears AA everywhere.
    ('SIGNAL', "Signal  (dark, cyan glow)", dict(
        _EX, brand_count=3, brand_1=_lin("131316"), brand_2=_lin("17303D"),
        brand_3=_lin("2E7F96"), color_stops=5, band_sharpness=1.5,
        wave_scale=0.20, distortion=3.5, noise_amount=0.36, noise_scale=0.45,
        noise_detail=3.0, blur_amount=12.0, stretch_x=1.25, rotation=-38.0,
        grain=0.0, grain_animate=False, dither_amount=1.4)),

    ('MIST', "Mist  (light, cool pastel)", dict(
        _EX, brand_count=3, brand_1=_lin("EEF2FA"), brand_2=_lin("C9D8F2"),
        brand_3=_lin("D8CFF0"), color_stops=5, band_sharpness=1.2,
        wave_scale=0.24, distortion=5.0, noise_amount=0.38, noise_scale=0.55,
        noise_detail=3.0, blur_amount=9.0, stretch_x=1.45, rotation=8.0,
        grain=0.035, grain_animate=True)),

    ('EMBER', "Ember  (dark, warm)", dict(
        _EX, brand_count=3, brand_1=_lin("0B0708"), brand_2=_lin("2E1114"),
        brand_3=_lin("D9552F"), color_stops=5, band_sharpness=1.7,
        wave_scale=0.24, distortion=4.5, noise_amount=0.40, noise_scale=0.48,
        noise_detail=3.0, blur_amount=10.0, stretch_x=1.4, rotation=-14.0,
        grain=0.0, grain_animate=False, dither_amount=1.4)),

    ('HOLO', "Holo Streak  (iridescent, simulated)", dict(
        _EX, pipeline='IRIDESCENT', film_ior=1.35, substrate_ior=1.0,
        film_thickness_max=1400.0, film_base=0.30, film_relief=0.18, sweep_amount=0.42,
        sweep_freq=6.0, film_strength=1.0, streak_angle=35.0, cover_freq=1.1,
        cover_width=0.18, cover_opacity=1.0, substrate_color=(0.045, 0.20, 0.38),
        paper_grain=0.40, iri_bump=0.15, iri_flat=True, grain=0.02, grain_animate=True)),
]


class WT_OT_example(bpy.types.Operator):
    bl_idname = "wavetex.example"
    bl_label = "Apply Example"
    bl_options = {'REGISTER', 'UNDO'}
    name_id: bpy.props.StringProperty()

    def execute(self, ctx):
        entry = next((e for e in EXAMPLES if e[0] == self.name_id), None)
        if entry is None:
            return {'CANCELLED'}
        data = dict(entry[2])
        if data.get('pipeline') == 'IRIDESCENT' and bpy.data.materials.get(IRI_MAT) is None:
            self.report({'ERROR'}, "Build the Iridescent Film pipeline first")
            return {'CANCELLED'}
        p = ctx.scene.wavetex
        need_lut = any(k in data and getattr(p, k) != data[k]
                       for k in ('film_ior', 'substrate_ior', 'film_thickness_max'))
        dict_to_props(p, data, ctx)
        if data.get('pipeline') == 'IRIDESCENT' and need_lut:
            bake_lut(ctx.scene, None)
            sync_iri(ctx)
        assign_pipeline(ctx)
        self.report({'INFO'}, "Applied %s" % entry[1])
        return {'FINISHED'}


def _srgb_decode(c):
    """sRGB transfer function, exactly as WCAG specifies it."""
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _rel_luminance(rgb_linear):
    """WCAG relative luminance. Inputs must be linear-light."""
    return (0.2126 * rgb_linear[..., 0] + 0.7152 * rgb_linear[..., 1]
            + 0.0722 * rgb_linear[..., 2])


def _contrast_ratio(l1, l2):
    hi, lo = np.maximum(l1, l2), np.minimum(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


class WT_OT_check_contrast(bpy.types.Operator):
    bl_idname = "wavetex.check_contrast"
    bl_label = "Check Text Contrast"
    bl_description = ("Render the current frame and measure real WCAG contrast between your "
                      "text colour and the worst spot on the background")

    def execute(self, ctx):
        sc = ctx.scene
        p = sc.wavetex
        path = os.path.join(bpy.app.tempdir, "wt_contrast.png")
        keep_path, keep_frame = sc.render.filepath, sc.render.resolution_percentage
        with _ImageSettings(sc) as s:
            s.file_format, s.color_mode, s.color_depth = 'PNG', 'RGB', '8'
            sc.render.filepath = path
            try:
                bpy.ops.render.render(write_still=True)
            finally:
                sc.render.filepath = keep_path
        img = bpy.data.images.load(path)
        # Non-Color stops Blender transforming on read, so we get exactly the
        # display-encoded values a browser would show - which is what WCAG
        # measures, after its own sRGB decode.
        img.colorspace_settings.name = 'Non-Color'
        buf = np.empty(len(img.pixels), dtype=np.float32)
        img.pixels.foreach_get(buf)
        bpy.data.images.remove(img)
        px = _srgb_decode(buf.reshape(-1, 4)[:, :3])

        bg_lum = _rel_luminance(px)
        # Blender stores colour properties linear already
        txt_lum = float(_rel_luminance(np.array([list(p.text_color)]))[0])
        ratios = _contrast_ratio(bg_lum, txt_lum)

        worst = float(ratios.min())
        median = float(np.median(ratios))
        # fraction of the frame that fails - a single dark corner matters less
        need = 3.0 if p.text_size_class == 'LARGE' else 4.5
        need_aaa = 4.5 if p.text_size_class == 'LARGE' else 7.0
        fail_pct = float((ratios < need).mean() * 100.0)

        # Internal luminance variance of the background itself, P5 to P95.
        # Measured on real product sites this lands between 1.07:1 (Linear) and
        # 2.12:1 (Raycast); 3:1 is where a background starts fighting content.
        p5, p95 = np.percentile(bg_lum, 5), np.percentile(bg_lum, 95)
        variance = float(_contrast_ratio(np.array(p95), np.array(p5)))
        if variance <= 2.0:
            vnote = "PASS flatness %.2f:1 (target 1.1-2.0)" % variance
        elif variance <= 3.0:
            vnote = "BUSY flatness %.2f:1 (over 2.0, under the 3.0 ceiling)" % variance
        else:
            vnote = "FAIL flatness %.2f:1 - too much luminance swing" % variance

        verdict = "PASS AA (worst %.2f:1)" % worst if worst >= need else \
                  "FAIL AA on %.1f%% of frame (worst %.2f:1, need %.1f)" % (fail_pct, worst, need)
        aaa = "PASS AAA" if worst >= need_aaa else "AAA needs %.1f:1" % need_aaa
        rep = "|".join([verdict, "%s | median %.2f:1" % (aaa, median), vnote])
        p.contrast_report = rep
        self.report({'INFO'} if worst >= need else {'WARNING'}, verdict)
        return {'FINISHED'}


def measure_field(ctx, samples=384):
    """Render the raw field as greyscale and return its sorted values.

    There is no way to read a shader value back directly, so the material is
    briefly reduced to 'field -> greyscale' with every post effect off, one
    small frame is rendered, and the original settings are restored.
    """
    sc = ctx.scene
    n = anodes()
    eq, steps, ramp = n['FieldEqualise'], n['FieldSteps'], n['GradientRamp']

    shape = n['FieldShape']
    saved = dict(
        rx=sc.render.resolution_x, ry=sc.render.resolution_y, fp=sc.render.filepath,
        interp=ramp.color_ramp.interpolation, si=steps.interpolation_type,
        eq_stops=[(e.position, tuple(e.color)) for e in eq.color_ramp.elements],
        stops=[(e.position, tuple(e.color)) for e in ramp.color_ramp.elements],
        smin=shape.inputs['From Min'].default_value,
        smax=shape.inputs['From Max'].default_value,
        tmin=steps.inputs['To Min'].default_value,
        tmax=steps.inputs['To Max'].default_value,
        vt=sc.view_settings.view_transform, nodes=sc.use_nodes)
    try:
        sc.use_nodes = False                     # bypass blur/grain/bloom entirely
        # Measure the RAW field. FieldShape's clamp piles pixels into atoms at
        # exactly 0 and 1, and equalisation cannot spread an atom - those
        # pixels would all land in one zone however the curve is drawn.
        shape.inputs['From Min'].default_value = 0.0
        shape.inputs['From Max'].default_value = 1.0
        sc.view_settings.view_transform = 'Standard'
        sc.render.resolution_x, sc.render.resolution_y = samples, int(samples * 9 / 16)
        # The quantiser also carries a zone-mode output bias; leave it in and the
        # curve gets calibrated against values the equaliser never actually sees.
        steps.interpolation_type = 'LINEAR'
        steps.inputs['To Min'].default_value = 0.0
        steps.inputs['To Max'].default_value = 1.0
        _ramp_set(eq, [(0.0, (0, 0, 0, 1)), (1.0, (1, 1, 1, 1))], 'LINEAR')
        _ramp_set(ramp, [(0.0, (0, 0, 0, 1)), (1.0, (1, 1, 1, 1))], 'LINEAR')
        path = os.path.join(bpy.app.tempdir, "wt_field_probe.png")
        sc.render.filepath = path
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.load(path)
        a = np.array(img.pixels[:]).reshape(-1, 4)[:, 0]
        bpy.data.images.remove(img)
        # undo the display transform to recover the linear field value
        a = np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
        return np.sort(a.astype(np.float64))
    finally:
        sc.use_nodes = saved['nodes']
        sc.view_settings.view_transform = saved['vt']
        sc.render.resolution_x, sc.render.resolution_y = saved['rx'], saved['ry']
        sc.render.filepath = saved['fp']
        steps.interpolation_type = saved['si']
        shape.inputs['From Min'].default_value = saved['smin']
        shape.inputs['From Max'].default_value = saved['smax']
        steps.inputs['To Min'].default_value = saved['tmin']
        steps.inputs['To Max'].default_value = saved['tmax']
        _ramp_set(eq, saved['eq_stops'], 'LINEAR')
        _ramp_set(ramp, saved['stops'], saved['interp'])


def _ramp_set(node, stops, interp):
    cr = node.color_ramp
    while len(cr.elements) > 1:
        cr.elements.remove(cr.elements[-1])
    cr.elements[0].position, cr.elements[0].color = stops[0][0], stops[0][1]
    for pos, col in stops[1:]:
        cr.elements.new(pos).color = col
    cr.interpolation = interp


# Film stocks, ordered fine -> coarse. `scale` is the enlargement needed to
# reach the same delivery frame: 16mm negative area is roughly a quarter of
# 35mm, so it is blown up about twice as much and the crystals come with it.
FILM_STOCKS = [
    ('FINE35',  "35mm Fine (50D)",   dict(grain=0.030, grain_size=0.55, grain_roughness=0.35,
                                          grain_scale=1.0, grain_chroma=0.22, grain_rolloff=1.0)),
    ('STD35',   "35mm Standard",     dict(grain=0.048, grain_size=0.85, grain_roughness=0.50,
                                          grain_scale=1.15, grain_chroma=0.30, grain_rolloff=0.95)),
    ('PUSH35',  "35mm Pushed (500T)", dict(grain=0.072, grain_size=1.15, grain_roughness=0.68,
                                           grain_scale=1.4, grain_chroma=0.38, grain_rolloff=0.85)),
    ('MM16',    "16mm",              dict(grain=0.085, grain_size=1.45, grain_roughness=0.62,
                                          grain_scale=2.0, grain_chroma=0.34, grain_rolloff=0.9)),
    ('SUPER8',  "Super 8",           dict(grain=0.115, grain_size=1.95, grain_roughness=0.75,
                                          grain_scale=3.0, grain_chroma=0.45, grain_rolloff=0.8)),
    ('BW400',   "B&W 400 (Tri-X)",   dict(grain=0.082, grain_size=1.35, grain_roughness=0.80,
                                          grain_scale=1.8, grain_chroma=0.0, grain_rolloff=0.9)),
    ('CLEAN',   "None",              dict(grain=0.0, grain_size=1.4, grain_roughness=0.5,
                                          grain_scale=1.0, grain_chroma=0.30, grain_rolloff=1.0)),
]


class WT_OT_film_stock(bpy.types.Operator):
    bl_idname = "wavetex.film_stock"
    bl_label = "Film Stock"
    bl_options = {'REGISTER', 'UNDO'}
    stock: bpy.props.StringProperty()

    def execute(self, ctx):
        entry = next((e for e in FILM_STOCKS if e[0] == self.stock), None)
        if entry is None:
            return {'CANCELLED'}
        p = ctx.scene.wavetex
        global _MUTE
        prev, _MUTE = _MUTE, True
        try:
            for k, v in entry[2].items():
                setattr(p, k, v)
        finally:
            _MUTE = prev
        u_grain_build(p, ctx)
        self.report({'INFO'}, entry[1])
        return {'FINISHED'}


class WT_OT_view_camera(bpy.types.Operator):
    bl_idname = "wavetex.view_camera"
    bl_label = "View Through Camera"
    bl_description = ("Look through the render camera. Outside the frame is blanked, "
                      "so what you see is exactly what renders")

    def execute(self, ctx):
        cam = ctx.scene.camera
        if cam:
            cam.data.show_passepartout = True
            cam.data.passepartout_alpha = 1.0
        # ctx.screen is not reliable when this runs from a script or a different
        # area, so walk every open window rather than trusting the caller.
        hits = 0
        for win in ctx.window_manager.windows:
            for area in win.screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                for space in area.spaces:
                    if space.type == 'VIEW_3D' and space.region_3d:
                        space.region_3d.view_perspective = 'CAMERA'
                        hits += 1
                area.tag_redraw()
        if not hits:
            self.report({'WARNING'}, "No 3D viewport found")
            return {'CANCELLED'}
        return {'FINISHED'}


class WT_OT_balance_zones(bpy.types.Operator):
    bl_idname = "wavetex.balance_zones"
    bl_label = "Balance Zones"
    bl_description = ("Measure the field and flatten its histogram so every palette "
                      "colour gets real area. Without this the outer zones can end up "
                      "with almost no pixels and simply disappear from the render")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        p = ctx.scene.wavetex
        if anodes() is None:
            self.report({'ERROR'}, "Aura pipeline is not built")
            return {'CANCELLED'}
        vals = measure_field(ctx)
        if vals.size < 16 or vals.ptp() < 1e-5:
            self.report({'ERROR'}, "Field is flat - nothing to balance")
            return {'CANCELLED'}

        # Target CDF: uniform, then bend so the ground zone owns the share the
        # designer asked for. Both reference styles lean on a dominant ground
        # (near-black chassis, or near-white paper) with smaller colour cores.
        k = 24
        qs = np.linspace(0.0, 1.0, k)
        src = np.interp(qs, np.linspace(0, 1, vals.size), vals)
        gw = p.aura_ground_weight
        if p.aura_steps > 1 and gw > 0.0:
            edge = 1.0 / p.aura_steps          # where zone 0 ends by default
            tgt = np.where(qs <= gw, qs / max(gw, 1e-6) * edge,
                           edge + (qs - gw) / max(1.0 - gw, 1e-6) * (1.0 - edge))
        else:
            tgt = qs
        stops = []
        last = -1.0
        for s, t in zip(src, tgt):
            s = min(1.0, max(0.0, float(s)))
            if s <= last:                       # ramp positions must increase
                s = min(1.0, last + 1e-4)
            last = s
            stops.append((s, (float(t), float(t), float(t), 1.0)))
        _ramp_set(anodes()['FieldEqualise'], stops, 'LINEAR')
        self.report({'INFO'}, "Balanced from %d samples" % vals.size)
        return {'FINISHED'}


class WT_OT_aura_seed_step(bpy.types.Operator):
    bl_idname = "wavetex.aura_seed_step"
    bl_label = "Step Composition"
    bl_options = {'REGISTER', 'UNDO'}
    delta: bpy.props.IntProperty(default=1)

    def execute(self, ctx):
        p = ctx.scene.wavetex
        p.aura_seed = max(0, p.aura_seed + self.delta)
        return {'FINISHED'}


class WT_LibraryItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name")
    pipeline: bpy.props.StringProperty(name="Pipeline")


class WT_UL_library(bpy.types.UIList):
    def draw_item(self, ctx, layout, data, item, icon, active_data, active_prop, index):
        icon_id = 'PHYSICS' if item.pipeline == 'IRIDESCENT' else 'MOD_WAVE'
        layout.label(text=item.name, icon=icon_id)


class WT_OT_lib_save(bpy.types.Operator):
    bl_idname = "wavetex.lib_save"
    bl_label = "Save Preset"
    bl_description = "Save the current look to your personal library (shared across .blend files)"
    name_str: bpy.props.StringProperty(name="Preset Name", default="My Look")
    overwrite: bpy.props.BoolProperty(name="Overwrite if it exists", default=False)

    def invoke(self, ctx, event):
        return ctx.window_manager.invoke_props_dialog(self)

    def execute(self, ctx):
        name = self.name_str.strip()
        if not name:
            self.report({'ERROR'}, "Give the preset a name")
            return {'CANCELLED'}
        data = library_load()
        if name in data and not self.overwrite:
            self.report({'ERROR'}, "'%s' already exists - tick Overwrite to replace it" % name)
            return {'CANCELLED'}
        data[name] = props_to_dict(ctx.scene.wavetex)
        if not library_write(data):
            self.report({'ERROR'}, "Could not write the library file")
            return {'CANCELLED'}
        library_refresh(ctx.scene)
        for i, it in enumerate(ctx.scene.wavetex_library):
            if it.name == name:
                ctx.scene.wavetex_library_index = i
        self.report({'INFO'}, "Saved '%s'" % name)
        return {'FINISHED'}


class WT_OT_lib_apply(bpy.types.Operator):
    bl_idname = "wavetex.lib_apply"
    bl_label = "Apply Preset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        sc = ctx.scene
        if not sc.wavetex_library:
            return {'CANCELLED'}
        item = sc.wavetex_library[sc.wavetex_library_index]
        data = library_load().get(item.name)
        if data is None:
            self.report({'ERROR'}, "'%s' is no longer in the library" % item.name)
            library_refresh(sc)
            return {'CANCELLED'}
        dict_to_props(sc.wavetex, data, ctx)
        assign_pipeline(ctx)
        self.report({'INFO'}, "Applied '%s'" % item.name)
        return {'FINISHED'}


class WT_OT_lib_delete(bpy.types.Operator):
    bl_idname = "wavetex.lib_delete"
    bl_label = "Delete Preset"
    bl_description = "Remove the selected preset from your library"

    def invoke(self, ctx, event):
        return ctx.window_manager.invoke_confirm(self, event)

    def execute(self, ctx):
        sc = ctx.scene
        if not sc.wavetex_library:
            return {'CANCELLED'}
        name = sc.wavetex_library[sc.wavetex_library_index].name
        data = library_load()
        if name not in data:
            library_refresh(sc)
            return {'CANCELLED'}
        del data[name]
        if not library_write(data):
            self.report({'ERROR'}, "Could not write the library file")
            return {'CANCELLED'}
        library_refresh(sc)
        self.report({'INFO'}, "Deleted '%s'" % name)
        return {'FINISHED'}


class WT_OT_lib_refresh(bpy.types.Operator):
    bl_idname = "wavetex.lib_refresh"
    bl_label = "Reload Library"

    def execute(self, ctx):
        library_refresh(ctx.scene)
        self.report({'INFO'}, "Library reloaded from %s" % library_path())
        return {'FINISHED'}


class WT_OT_iri_build(bpy.types.Operator):
    bl_idname = "wavetex.iri_build"
    bl_label = "Bake Simulation + Build"
    bl_description = ("Run the wave simulation, bake the loop to disk, compute the thin-film "
                      "LUT and build the iridescent material")

    def execute(self, ctx):
        build_stage(ctx)
        bake_lut(ctx.scene, self.report)
        bake_simulation(ctx.scene, self.report)
        build_iridescent_material(ctx.scene)
        ctx.scene.wavetex.pipeline = 'IRIDESCENT'
        assign_pipeline(ctx)
        sync_iri(ctx)
        sync_fx(ctx)
        return {'FINISHED'}


class WT_OT_iri_bake_sim(bpy.types.Operator):
    bl_idname = "wavetex.iri_bake_sim"
    bl_label = "Re-bake Simulation"

    def execute(self, ctx):
        bake_simulation(ctx.scene, self.report)
        sync_iri(ctx)
        return {'FINISHED'}


class WT_OT_iri_bake_lut(bpy.types.Operator):
    bl_idname = "wavetex.iri_bake_lut"
    bl_label = "Rebuild Film LUT"

    def execute(self, ctx):
        bake_lut(ctx.scene, self.report)
        sync_iri(ctx)
        return {'FINISHED'}


class WT_OT_iri_preset(bpy.types.Operator):
    bl_idname = "wavetex.iri_preset"
    bl_label = "Apply Iridescence Preset"
    bl_options = {'REGISTER', 'UNDO'}
    name_id: bpy.props.StringProperty()

    def execute(self, ctx):
        p = ctx.scene.wavetex
        vals = IRI_PRESETS[self.name_id]
        need_lut = any(k in vals and getattr(p, k) != vals[k]
                       for k in ('film_ior', 'substrate_ior', 'film_thickness_max'))
        for k, v in vals.items():
            setattr(p, k, v)
        if need_lut and bpy.data.images.get("WT_FilmLUT"):
            bake_lut(ctx.scene, None)     # IOR changes alter the physics, so recompute
        sync_iri(ctx)
        return {'FINISHED'}


class WT_OT_iri_random(bpy.types.Operator):
    bl_idname = "wavetex.iri_random"
    bl_label = "Randomize Look"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, ctx):
        p = ctx.scene.wavetex
        p.streak_angle = random.uniform(-180, 180)
        p.cover_width = random.uniform(0.15, 0.8)
        p.cover_freq = random.uniform(0.4, 1.6)
        p.sweep_freq = random.uniform(1.5, 7.0)
        p.film_base = random.uniform(0.2, 0.6)
        p.substrate_color = (random.uniform(0, 0.3), random.uniform(0, 0.35), random.uniform(0.1, 0.5))
        sync_iri(ctx)
        return {'FINISHED'}


FX_PRESETS = {
    'CLEAN': dict(blur_amount=0.0, painterly=0.0, pixelate=1, bloom=0.0, posterize_steps=0,
                  dither_mode='NOISE', dither_amount=1.0, grain=0.0, grain_animate=False,
                  chromatic=0.0, lens_distort=0.0, vignette=0.0, exposure=0.0),
    'SOFT': dict(blur_amount=4.0, painterly=0.0, pixelate=1, bloom=0.25, bloom_threshold=0.7,
                 posterize_steps=0, dither_mode='NOISE', dither_amount=1.0, grain=0.04,
                 grain_animate=True, chromatic=0.0, lens_distort=0.0, vignette=0.35, exposure=0.0),
    'RETRO': dict(blur_amount=0.0, painterly=0.0, pixelate=3, bloom=0.0, posterize_steps=5,
                  dither_mode='ORDERED', dither_amount=1.0, grain=0.0, grain_animate=False,
                  chromatic=0.0, lens_distort=0.0, vignette=0.25, exposure=0.0),
    'FILMIC': dict(blur_amount=2.0, painterly=0.0, pixelate=1, bloom=0.35, bloom_threshold=0.75,
                   posterize_steps=0, dither_mode='NOISE', dither_amount=1.0, grain=0.12,
                   grain_animate=True, chromatic=0.02, lens_distort=0.02, vignette=0.5, exposure=0.0),
    'PAINT': dict(blur_amount=0.0, painterly=12.0, pixelate=1, bloom=0.15, posterize_steps=0,
                  dither_mode='NOISE', dither_amount=1.0, grain=0.05, grain_animate=True,
                  chromatic=0.0, lens_distort=0.0, vignette=0.3, exposure=0.0),
    'VHS': dict(blur_amount=1.5, painterly=0.0, pixelate=2, bloom=0.3, bloom_threshold=0.6,
                posterize_steps=8, dither_mode='ORDERED', dither_amount=1.2, grain=0.2,
                grain_animate=True, chromatic=0.12, lens_distort=0.05, vignette=0.55, exposure=0.0),
}

WEB_SIZES = [
    ("1920 x 1080", 1920, 1080), ("2560 x 1440", 2560, 1440),
    ("1080 x 1080", 1080, 1080), ("1080 x 1920", 1080, 1920),
    ("1024 x 1024", 1024, 1024), ("3840 x 2160", 3840, 2160),
]


class WT_OT_fx_preset(bpy.types.Operator):
    bl_idname = "wavetex.fx_preset"
    bl_label = "Apply Effect Preset"
    bl_options = {'REGISTER', 'UNDO'}
    name_id: bpy.props.StringProperty()

    def execute(self, ctx):
        p = ctx.scene.wavetex
        for k, v in FX_PRESETS[self.name_id].items():
            setattr(p, k, v)
        return {'FINISHED'}


class WT_OT_rebuild_patterns(bpy.types.Operator):
    bl_idname = "wavetex.rebuild_patterns"
    bl_label = "Rebuild Dither Patterns"
    bl_options = {'REGISTER'}

    def execute(self, ctx):
        r = ctx.scene.render
        r.resolution_percentage = 100
        sync_fx(ctx)     # u_fx_scrim rebuilds every pattern at the right size
        self.report({'INFO'}, "Patterns rebuilt at %dx%d" % (r.resolution_x, r.resolution_y))
        return {'FINISHED'}


class WT_OT_set_size(bpy.types.Operator):
    bl_idname = "wavetex.set_size"
    bl_label = "Set Output Size"
    bl_options = {'REGISTER', 'UNDO'}
    w: bpy.props.IntProperty()
    h: bpy.props.IntProperty()

    def execute(self, ctx):
        r = ctx.scene.render
        r.resolution_x, r.resolution_y = self.w, self.h
        r.resolution_percentage = 100
        cam = ctx.scene.camera
        if cam and cam.data.type == 'ORTHO':
            # ortho_scale spans the LARGER image dimension, so 2.0 on a 2x2 plane
            # fills the frame at any aspect ratio - it just crops the short side.
            cam.data.ortho_scale = 2.0
        build_patterns(self.w, self.h)      # patterns must match the new pixel grid
        sync_fx(ctx)
        return {'FINISHED'}


def _hex(rgb_linear):
    enc = np.where(rgb_linear <= 0.0031308, rgb_linear * 12.92,
                   1.055 * np.clip(rgb_linear, 0, None) ** (1 / 2.4) - 0.055)
    v = np.clip(enc * 255.0, 0, 255).astype(int)
    return "#%02x%02x%02x" % tuple(v)


def sample_palette(scene):
    """Render one frame and pull out a CSS fallback colour plus the extremes."""
    path = os.path.join(bpy.app.tempdir, "wt_palette.png")
    keep = scene.render.filepath
    with _ImageSettings(scene) as s:
        s.file_format, s.color_mode, s.color_depth = 'PNG', 'RGB', '8'
        scene.render.filepath = path
        try:
            bpy.ops.render.render(write_still=True)
        finally:
            scene.render.filepath = keep
    img = bpy.data.images.load(path)
    img.colorspace_settings.name = 'Non-Color'
    buf = np.empty(len(img.pixels), dtype=np.float32)
    img.pixels.foreach_get(buf)
    bpy.data.images.remove(img)
    px = _srgb_decode(buf.reshape(-1, 4)[:, :3])
    lum = _rel_luminance(px)
    return {
        'average': _hex(px.mean(axis=0)),
        'darkest': _hex(px[np.argmin(lum)]),
        'lightest': _hex(px[np.argmax(lum)]),
        'luminance_min': round(float(lum.min()), 4),
        'luminance_max': round(float(lum.max()), 4),
    }


class WT_OT_poster(bpy.types.Operator):
    bl_idname = "wavetex.poster"
    bl_label = "Render Poster Frame"
    bl_description = ("Render a single still next to the loop. Browsers show it before the video "
                      "loads and it is the fallback for prefers-reduced-motion")

    def execute(self, ctx):
        sc = ctx.scene
        p = sc.wavetex
        keep_frame, keep_path = sc.frame_current, sc.render.filepath
        base = bpy.path.abspath(sc.render.filepath) or os.path.join(bpy.app.tempdir, "wavetex_")
        folder = os.path.dirname(base) or bpy.app.tempdir
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, "poster.png")
        sc.frame_set(min(max(1, p.poster_frame), p.loop_frames))
        with _ImageSettings(sc) as s:
            s.file_format = 'PNG'
            s.color_mode = 'RGBA' if p.transparent_bg else 'RGB'
            s.color_depth = '8'
            sc.render.filepath = out
            try:
                bpy.ops.render.render(write_still=True)
            finally:
                sc.render.filepath = keep_path
        sc.frame_set(keep_frame)
        self.report({'INFO'}, "Poster written to %s" % out)
        return {'FINISHED'}


class WT_OT_handoff(bpy.types.Operator):
    bl_idname = "wavetex.handoff"
    bl_label = "Write Handoff Notes"
    bl_description = "Sample the render and write a JSON + CSS snippet for the developer"

    def execute(self, ctx):
        sc = ctx.scene
        p = sc.wavetex
        info = sample_palette(sc)
        fps = sc.render.fps / sc.render.fps_base
        base = bpy.path.abspath(sc.render.filepath) or os.path.join(bpy.app.tempdir, "wavetex_")
        folder = os.path.dirname(base) or bpy.app.tempdir
        os.makedirs(folder, exist_ok=True)

        data = {
            'pipeline': p.pipeline,
            'resolution': [sc.render.resolution_x, sc.render.resolution_y],
            'loop_frames': p.loop_frames,
            'loop_seconds': round(p.loop_frames / fps, 3),
            'fps': round(fps, 3),
            'format': p.export_format,
            'transparent': p.transparent_bg,
            'colors': info,
        }
        with open(os.path.join(folder, "handoff.json"), 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2)

        alpha_note = ("\n  NOTE: VP9 alpha does not work in Safari - it composites on black.\n"
                      "  Ship an opaque loop, or add an HEVC-with-alpha .mov source first.\n"
                      if (p.transparent_bg and p.export_format == 'WEBM') else "")
        html = (
            "<!-- Poster is the LCP element for a <video>, so preload it. -->\n"
            '<link rel="preload" as="image" href="poster.png" fetchpriority="high">\n\n'
            '<div class="bg">\n'
            '  <video autoplay muted loop playsinline preload="none"\n'
            '         poster="poster.png" aria-hidden="true">\n'
            '    <source src="loop.webm" type="video/webm">\n'
            '    <source src="loop.mp4"  type="video/mp4">\n'
            '  </video>\n'
            '  <button class="bg-pause" aria-label="Pause background">Pause</button>\n'
            '</div>\n'
        )
        css = (
            "/* Wave Texture Maker handoff */\n"
            ".bg {\n"
            "  background-color: %s;            /* fallback before the loop loads */\n"
            "  position: relative; isolation: isolate;\n"
            "}\n"
            ".bg video {\n"
            "  position: absolute; inset: 0;\n"
            "  width: 100%%; height: 100%%; object-fit: cover;\n"
            "}\n"
            "@media (prefers-reduced-motion: reduce) {\n"
            "  .bg video { display: none; }     /* poster carries the design */\n"
            "}\n"
            "\n"
            "/* WCAG 2.2.2 Pause Stop Hide (Level A): a decorative loop that runs\n"
            "   longer than 5s behind readable content needs a control for EVERY\n"
            "   user. prefers-reduced-motion alone does not satisfy it - that is\n"
            "   the sufficient technique for 2.3.3, a different criterion.\n"
            "   Keep .bg-pause in the DOM, or stop the loop after 5s. */\n"
            "\n"
            "/* loop      : %.2fs at %.0f fps (%d frames)\n"
            "   output    : %dx%d, %s\n"
            "   luminance : %.3f min .. %.3f max\n"
            "   darkest   : %s      lightest: %s%s */\n"
        ) % (info['average'], data['loop_seconds'], fps, p.loop_frames,
             sc.render.resolution_x, sc.render.resolution_y, p.export_format,
             info['luminance_min'], info['luminance_max'],
             info['darkest'], info['lightest'], alpha_note)
        with open(os.path.join(folder, "background.css"), 'w', encoding='utf-8') as fh:
            fh.write(css)
        with open(os.path.join(folder, "background.html"), 'w', encoding='utf-8') as fh:
            fh.write(html)
        self.report({'INFO'}, "Handoff written (fallback %s) to %s" % (info['average'], folder))
        return {'FINISHED'}


class WT_OT_export(bpy.types.Operator):
    bl_idname = "wavetex.export"
    bl_label = "Render Loop"

    def execute(self, ctx):
        sc = ctx.scene
        p = sc.wavetex
        r = sc.render
        sc.frame_start = 1
        sc.frame_end = p.loop_frames

        if bpy.data.images.get("WT_DitherBayer") is None or \
                tuple(bpy.data.images["WT_DitherBayer"].size) != (r.resolution_x, r.resolution_y):
            build_patterns(r.resolution_x, r.resolution_y)
            sync_fx(ctx)

        if not r.filepath:
            r.filepath = "//wavetex_out/frame_"

        if p.export_format == 'PNG':
            r.image_settings.file_format = 'PNG'
            r.image_settings.color_mode = 'RGBA' if p.transparent_bg else 'RGB'
        else:
            r.image_settings.file_format = 'FFMPEG'
            r.ffmpeg.gopsize = min(18, p.loop_frames)
            # A silent audio track is pure waste on a muted background loop -
            # measured at 28-48% of file size on real production assets.
            r.ffmpeg.audio_codec = 'NONE'
            if p.export_format == 'MP4':
                r.ffmpeg.format = 'MPEG4'
                r.ffmpeg.codec = 'H264'
                r.ffmpeg.constant_rate_factor = 'HIGH'
                r.image_settings.color_mode = 'RGB'
            else:
                r.ffmpeg.format = 'WEBM'
                r.ffmpeg.codec = 'WEBM'
                r.image_settings.color_mode = 'RGBA' if p.transparent_bg else 'RGB'
            r.ffmpeg.ffmpeg_preset = 'GOOD'

        bpy.ops.render.render(animation=True)
        self.report({'INFO'}, "Rendered %d frames (%s) to %s"
                    % (sc.frame_end, p.export_format, r.filepath))
        return {'FINISHED'}


# ------------------------------------------------------------------ UI ------

class Base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Wave Tex"


class Pipe:
    """Shows a panel only for the pipelines that actually own those controls.

    Adding a pipeline means: give its panels a PIPES set, add the enum entry,
    and register a material builder. Nothing else in the UI needs to know.
    """
    PIPES = set()

    @classmethod
    def poll(cls, ctx):
        return ctx.scene.wavetex.pipeline in cls.PIPES


class WT_PT_main(Base, bpy.types.Panel):
    bl_idname = "WT_PT_main"
    bl_label = "Wave Texture Maker"

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        if bpy.data.materials.get(MAT_NAME) is None:
            lay.operator("wavetex.setup", icon='ADD')
            return

        # Pipeline switcher sits first and stays first. All three renderers are
        # always present in the file; switching just re-points the plane, so
        # nothing is lost by moving between them.
        box = lay.box()
        box.label(text="Pipeline", icon='NODETREE')
        col = box.column(align=True)
        col.scale_y = 1.25
        col.prop(p, "pipeline", expand=True)
        if p.pipeline == 'IRIDESCENT' and bpy.data.materials.get(IRI_MAT) is None:
            box.operator("wavetex.iri_build", text="Build Iridescent Film", icon='MOD_FLUIDSIM')
            box.label(text="Not built yet", icon='INFO')

        lay.separator()
        row = lay.row(align=True)
        row.scale_y = 1.4
        row.operator("wavetex.rewind", text="", icon='REW')
        row.operator("wavetex.play_loop", text="Play Loop", icon='PLAY')
        row.operator("wavetex.stop", text="", icon='PAUSE')
        lay.prop(p, "loop_frames")
        lay.label(text="Frame %d / %d" % (ctx.scene.frame_current, ctx.scene.frame_end))
        lay.operator("wavetex.view_camera", icon='CAMERA_DATA')
        lay.separator()
        if p.pipeline == 'WAVE':
            lay.prop(p, "shading_mode", expand=True)
        lay.prop(p, "view_transform", text="")
        r = lay.row(align=True)
        r.operator("wavetex.surprise", icon='SHADERFX')
        r.operator("wavetex.sync", text="", icon='FILE_REFRESH')


class WT_PT_library(Base, bpy.types.Panel):
    bl_idname = "WT_PT_library"
    bl_parent_id = "WT_PT_main"
    bl_label = "Example Library"

    def draw(self, ctx):
        lay = self.layout
        lay.label(text="Curated looks for web backgrounds")
        col = lay.column(align=True)
        for key, label, _ in EXAMPLES:
            col.operator("wavetex.example", text=label).name_id = key


class WT_PT_mypresets(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_library"
    bl_label = "My Presets"

    _seeded = False

    def draw(self, ctx):
        sc = ctx.scene
        lay = self.layout
        if not WT_PT_mypresets._seeded:
            WT_PT_mypresets._seeded = True
            library_refresh(sc)        # first safe moment to touch bpy.data
        row = lay.row()
        row.template_list("WT_UL_library", "", sc, "wavetex_library",
                          sc, "wavetex_library_index", rows=4)
        side = row.column(align=True)
        side.operator("wavetex.lib_save", text="", icon='ADD')
        side.operator("wavetex.lib_delete", text="", icon='REMOVE')
        side.separator()
        side.operator("wavetex.lib_refresh", text="", icon='FILE_REFRESH')
        r = lay.row()
        r.enabled = len(sc.wavetex_library) > 0
        r.operator("wavetex.lib_apply", icon='CHECKMARK')


class WT_PT_presets(Pipe, Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Wave Presets"
    PIPES = {'WAVE'}

    def draw(self, ctx):
        g = self.layout.grid_flow(columns=2, even_columns=True)
        for key, label, icon in [('OCEAN', "Ocean", 'MOD_FLUIDSIM'),
                                 ('LAVA', "Lava", 'MATFLUID'),
                                 ('RIPPLE', "Ripple", 'MOD_WAVE'),
                                 ('RETRO', "Retro Bands", 'SEQ_CHROMA_SCOPE'),
                                 ('PLASMA', "Plasma", 'SHADERFX'),
                                 ('MONOSILK', "Mono Silk", 'MATSPHERE')]:
            g.operator("wavetex.preset", text=label, icon=icon).name_id = key


class WT_PT_aura(Pipe, Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Aura Flow"
    PIPES = {'AURA'}

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        c = lay.column(align=True)
        c.prop(p, "aura_field_scale")
        c.prop(p, "aura_scale")
        c.prop(p, "aura_rotation")
        c = lay.column(align=True)
        c.prop(p, "aura_warp")
        c.prop(p, "aura_warp_scale")
        c = lay.column(align=True)
        c.prop(p, "aura_detail")
        c.prop(p, "aura_roughness")
        c.prop(p, "aura_contrast")
        r = lay.row(align=True)
        r.prop(p, "aura_seed")
        r.operator("wavetex.aura_seed_step", text="", icon='TRIA_LEFT').delta = -1
        r.operator("wavetex.aura_seed_step", text="", icon='TRIA_RIGHT').delta = 1
        c = lay.column(align=True)
        c.prop(p, "aura_steps")
        c.prop(p, "aura_ground_weight")
        lay.operator("wavetex.balance_zones", icon='SEQ_HISTOGRAM')
        c = lay.column(align=True)
        c.prop(p, "aura_strength")
        c.prop(p, "aura_drift")


class WT_PT_wave(Pipe, Base, bpy.types.Panel):
    bl_idname = "WT_PT_wave"
    bl_parent_id = "WT_PT_main"
    bl_label = "Wave Shape"
    PIPES = {'WAVE'}

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        lay.prop(p, "wave_style", expand=True)
        c = lay.column(align=True)
        c.prop(p, "wave_scale")
        c.prop(p, "distortion")
        c.prop(p, "detail")
        c.prop(p, "speed")


class WT_PT_transform(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_wave"
    bl_label = "Transform"

    def draw(self, ctx):
        p = ctx.scene.wavetex
        c = self.layout.column(align=True)
        c.prop(p, "rotation")
        r = c.row(align=True)
        r.prop(p, "offset_x", text="Off X")
        r.prop(p, "offset_y", text="Y")
        r2 = c.row(align=True)
        r2.prop(p, "stretch_x", text="Stretch X")
        r2.prop(p, "stretch_y", text="Y")


class WT_PT_color(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Color / Gradient"

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        lay.prop(p, "palette_mode", expand=True)
        if p.palette_mode == 'BRAND':
            lay.prop(p, "brand_count")
            r = lay.row(align=True)
            for i in range(p.brand_count):
                r.prop(p, "brand_%d" % (i + 1), text="")
            lay.prop(p, "brand_lift")
        else:
            row = lay.row(align=True)
            row.operator("wavetex.seed_step", text="", icon='TRIA_LEFT').delta = -1
            row.prop(p, "seed", text="Seed")
            row.operator("wavetex.seed_step", text="", icon='TRIA_RIGHT').delta = 1
            row.operator("wavetex.random_seed", text="", icon='FILE_REFRESH')
            r2 = lay.row(align=True)
            r2.prop(p, "harmony", text="")
            r2.operator("wavetex.cycle_harmony", text="", icon='LOOP_FORWARDS')
        c = lay.column(align=True)
        c.prop(p, "saturation")
        c.prop(p, "brightness")
        c.prop(p, "color_stops")
        lay.prop(p, "gradient_interp", text="Blend")
        c2 = lay.column(align=True)
        c2.prop(p, "color_cycles")
        c2.prop(p, "band_sharpness")
        lay.operator("wavetex.reverse_gradient", icon='ARROW_LEFTRIGHT')
        mat = bpy.data.materials.get(MAT_NAME)
        if mat:
            lay.template_color_ramp(mat.node_tree.nodes['GradientRamp'], "color_ramp", expand=True)


class WT_PT_filter(Pipe, Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Color Filters"
    PIPES = {'WAVE'}

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        c = lay.column(align=True)
        c.prop(p, "hue_shift")
        c.prop(p, "filter_sat")
        c.prop(p, "filter_value")
        c.prop(p, "contrast")
        c.prop(p, "filter_bright")
        lay.separator()
        r = lay.row(align=True)
        r.prop(p, "tint_color", text="")
        r.prop(p, "tint_amount")
        r2 = lay.row(align=True)
        r2.operator("wavetex.random_filter", icon='FILE_REFRESH')
        r2.operator("wavetex.reset_filter", icon='LOOP_BACK')


class WT_PT_noise(Pipe, Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Noise Overlay"
    PIPES = {'WAVE'}

    def draw(self, ctx):
        p = ctx.scene.wavetex
        c = self.layout.column(align=True)
        c.prop(p, "noise_amount")
        c.prop(p, "noise_scale")
        c.prop(p, "noise_detail")
        c.prop(p, "noise_drift")


class WT_PT_overlay(Pipe, Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Big Overlay"
    PIPES = {'WAVE'}

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        lay.prop(p, "overlay_type", expand=True)
        c = lay.column(align=True)
        c.enabled = p.overlay_type != 'NONE'
        c.prop(p, "overlay_amount")
        c.prop(p, "overlay_scale")


class WT_PT_surface(Pipe, Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Normal / Surface"
    PIPES = {'WAVE'}

    def draw(self, ctx):
        p = ctx.scene.wavetex
        c = self.layout.column(align=True)
        c.prop(p, "bump_strength")
        c.prop(p, "emission")
        c.prop(p, "roughness")
        c.prop(p, "metallic")
        if p.shading_mode == 'FLAT':
            self.layout.label(text="Normals need 'Lit + Normals' mode", icon='INFO')


class WT_PT_iri(Pipe, Base, bpy.types.Panel):
    bl_idname = "WT_PT_iri"
    bl_parent_id = "WT_PT_main"
    bl_label = "Iridescent Film (Simulated)"
    PIPES = {'IRIDESCENT'}

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        if bpy.data.materials.get(IRI_MAT) is None:
            lay.operator("wavetex.iri_build", icon='PHYSICS')
            lay.label(text="Bakes a looping wave sim to disk", icon='INFO')
            return
        lay.prop(p, "pipeline", expand=True)
        g = lay.grid_flow(columns=2, even_columns=True)
        for key, label in [('FOIL', "Holo Foil"), ('STREAK', "Paper Streak"),
                           ('OIL', "Oil Slick"), ('BUBBLE', "Soap Bubble"), ('PEARL', "Pearl")]:
            g.operator("wavetex.iri_preset", text=label).name_id = key
        lay.operator("wavetex.iri_random", icon='FILE_REFRESH')


class WT_PT_iri_film(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_iri"
    bl_label = "Thin Film Physics"

    @classmethod
    def poll(cls, ctx):
        return bpy.data.materials.get(IRI_MAT) is not None

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        c = lay.column(align=True)
        c.prop(p, "film_ior")
        c.prop(p, "substrate_ior")
        c.prop(p, "film_angle")
        c.prop(p, "film_thickness_max")
        lay.operator("wavetex.iri_bake_lut", icon='FILE_REFRESH')
        lay.separator()
        c2 = lay.column(align=True)
        c2.prop(p, "film_base")
        c2.prop(p, "film_relief")
        c2.prop(p, "sweep_amount")
        c2.prop(p, "sweep_freq")
        c2.prop(p, "film_strength")


class WT_PT_iri_streak(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_iri"
    bl_label = "Streak / Coverage"

    @classmethod
    def poll(cls, ctx):
        return bpy.data.materials.get(IRI_MAT) is not None

    def draw(self, ctx):
        p = ctx.scene.wavetex
        c = self.layout.column(align=True)
        c.prop(p, "streak_angle")
        c.prop(p, "cover_width")
        c.prop(p, "cover_freq")
        c.prop(p, "cover_opacity")


class WT_PT_iri_sim(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_iri"
    bl_label = "Wave Simulation"

    @classmethod
    def poll(cls, ctx):
        return bpy.data.materials.get(IRI_MAT) is not None

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        lay.prop(p, "sim_res")
        c = lay.column(align=True)
        c.prop(p, "sim_domain")
        c.prop(p, "sim_wind")
        c.prop(p, "sim_wind_angle")
        c.prop(p, "sim_capillary")
        c.prop(p, "sim_duration")
        c.prop(p, "sim_seed")
        lay.operator("wavetex.iri_bake_sim", icon='PHYSICS')
        c2 = lay.column(align=True)
        c2.prop(p, "sim_tiling")
        c2.prop(p, "iri_rotation")


class WT_PT_iri_substrate(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_iri"
    bl_label = "Substrate & Surface"

    @classmethod
    def poll(cls, ctx):
        return bpy.data.materials.get(IRI_MAT) is not None

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        lay.prop(p, "substrate_color", text="")
        c = lay.column(align=True)
        c.prop(p, "paper_grain")
        c.prop(p, "paper_scale")
        c.prop(p, "paper_stretch")
        c.prop(p, "paper_tooth")
        lay.separator()
        lay.prop(p, "iri_flat")
        c2 = lay.column(align=True)
        c2.prop(p, "iri_bump")
        c2.prop(p, "iri_glow")
        sub = c2.column(align=True)
        sub.enabled = not p.iri_flat
        sub.prop(p, "iri_roughness")


class WT_PT_fx(Base, bpy.types.Panel):
    bl_idname = "WT_PT_fx"
    bl_parent_id = "WT_PT_main"
    bl_label = "Global Effects"

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        lay.prop(p, "viewport_fx", text="Live Preview")
        g = lay.grid_flow(columns=2, even_columns=True)
        for key, label, icon in [('CLEAN', "Clean", 'SHADING_SOLID'),
                                 ('SOFT', "Soft Gradient", 'MOD_SMOOTH'),
                                 ('RETRO', "Retro Dither", 'IMAGE_ZDEPTH'),
                                 ('FILMIC', "Filmic", 'CAMERA_DATA'),
                                 ('PAINT', "Painterly", 'BRUSH_DATA'),
                                 ('VHS', "VHS", 'SEQ_PREVIEW')]:
            g.operator("wavetex.fx_preset", text=label, icon=icon).name_id = key


class WT_PT_fx_dither(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_fx"
    bl_label = "Dither & Posterize"

    def draw_header(self, ctx):
        self.layout.prop(ctx.scene.wavetex, "use_dither", text="")

    def draw(self, ctx):
        p = ctx.scene.wavetex
        self.layout.active = p.use_dither
        lay = self.layout
        lay.prop(p, "posterize_steps")
        lay.prop(p, "dither_mode", text="")
        c = lay.column()
        c.enabled = p.dither_mode != 'NONE'
        c.prop(p, "dither_amount")
        lay.prop(ctx.scene.render, "dither_intensity", text="8-bit Output Dither")
        lay.operator("wavetex.rebuild_patterns", icon='FILE_REFRESH')


class WT_PT_fx_blur(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_fx"
    bl_label = "Blur & Bloom"

    def draw_header(self, ctx):
        self.layout.prop(ctx.scene.wavetex, "use_blur", text="")

    def draw(self, ctx):
        p = ctx.scene.wavetex
        self.layout.active = p.use_blur
        c = self.layout.column(align=True)
        c.prop(p, "blur_amount")
        c.prop(p, "painterly")
        c.prop(p, "pixelate")
        c2 = self.layout.column(align=True)
        c2.prop(p, "bloom")
        sub = c2.column(align=True)
        sub.enabled = p.bloom > 0.0
        sub.prop(p, "bloom_threshold")
        sub.prop(p, "bloom_size")


class WT_PT_fx_lens(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_fx"
    bl_label = "Lens, Grain & Vignette"

    def draw_header(self, ctx):
        self.layout.prop(ctx.scene.wavetex, "use_lens", text="")

    def draw(self, ctx):
        p = ctx.scene.wavetex
        self.layout.active = p.use_lens
        lay = self.layout
        c = lay.column(align=True)
        c.prop(p, "chromatic")
        c.prop(p, "lens_distort")
        c2 = lay.column(align=True)
        c2.label(text="Film grain has its own panel below")
        r = lay.row(align=True)
        r.prop(p, "use_vignette", text="")
        r.label(text="Vignette")
        c3 = lay.column(align=True)
        c3.active = p.use_vignette
        c3.prop(p, "vignette")
        sub = c3.column(align=True)
        sub.enabled = p.vignette > 0.0
        sub.prop(p, "vignette_size")
        sub.prop(p, "vignette_softness")
        lay.prop(p, "exposure")


class WT_PT_grain(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_fx"
    bl_label = "Film Grain"

    def draw_header(self, ctx):
        self.layout.prop(ctx.scene.wavetex, "use_grain", text="")

    def draw(self, ctx):
        p = ctx.scene.wavetex
        self.layout.active = p.use_grain
        lay = self.layout
        col = lay.column(align=True)
        for key, label, _ in FILM_STOCKS:
            col.operator("wavetex.film_stock", text=label).stock = key
        lay.separator()
        c = lay.column(align=True)
        c.prop(p, "grain", text="Amount")
        c.prop(p, "grain_size")
        c.prop(p, "grain_roughness")
        c.prop(p, "grain_scale")
        c = lay.column(align=True)
        c.prop(p, "grain_chroma")
        c.prop(p, "grain_rolloff")
        lay.prop(p, "grain_animate", toggle=True,
                 icon='PLAY' if p.grain_animate else 'PAUSE')


class WT_PT_legibility(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Legibility & Layout"

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        c = lay.column(align=True)
        r = lay.row(align=True)
        r.prop(p, "use_tone", text="")
        r.label(text="Tone Range")
        c.active = p.use_tone
        c.prop(p, "tone_floor")
        c.prop(p, "tone_ceiling")
        lay.separator()
        r = lay.row(align=True)
        r.prop(p, "use_scrim", text="")
        r.label(text="Scrim")
        sc_col = lay.column(align=True)
        sc_col.active = p.use_scrim
        sc_col.prop(p, "scrim_strength")
        box = lay.column(align=True)
        box.enabled = p.scrim_strength > 0.0
        box.prop(p, "scrim_dir", text="")
        box.prop(p, "scrim_color", text="")
        box.prop(p, "scrim_coverage")
        box.prop(p, "scrim_softness")
        lay.separator()
        lay.prop(p, "edge_fade")
        if p.edge_fade and not p.transparent_bg:
            lay.label(text="Edge fade needs Transparent Background", icon='ERROR')
        e = lay.column(align=True)
        e.enabled = p.edge_fade
        e.prop(p, "edge_inset")
        e.prop(p, "edge_softness")


class WT_PT_contrast(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_legibility"
    bl_label = "Contrast Check"

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        r = lay.row(align=True)
        r.prop(p, "text_color", text="")
        r.prop(p, "text_size_class", text="")
        lay.operator("wavetex.check_contrast", icon='FONT_DATA')
        if p.contrast_report:
            for line in p.contrast_report.split("|"):
                if not line:
                    continue
                icon = 'CHECKMARK' if line.startswith("PASS") else (
                    'ERROR' if line.startswith("FAIL") else 'INFO')
                lay.label(text=line, icon=icon)


class WT_PT_export(Base, bpy.types.Panel):
    bl_parent_id = "WT_PT_main"
    bl_label = "Web Export"

    def draw(self, ctx):
        p = ctx.scene.wavetex
        lay = self.layout
        lay.prop(p, "transparent_bg")
        lay.prop(p, "export_format", text="")
        lay.label(text="Output Size")
        g = lay.grid_flow(columns=2, even_columns=True)
        for label, w, h in WEB_SIZES:
            op = g.operator("wavetex.set_size", text=label)
            op.w, op.h = w, h
        r = lay.row(align=True)
        r.prop(ctx.scene.render, "resolution_x", text="W")
        r.prop(ctx.scene.render, "resolution_y", text="H")
        lay.prop(ctx.scene.render, "filepath", text="Folder")
        lay.separator()
        fps = ctx.scene.render.fps / ctx.scene.render.fps_base
        lay.label(text="Loop: %d frames = %.2fs at %.0f fps"
                       % (p.loop_frames, p.loop_frames / fps, fps))
        lay.prop(p, "poster_frame")
        col = lay.column(align=True)
        col.operator("wavetex.export", icon='RENDER_ANIMATION')
        col.operator("wavetex.poster", icon='IMAGE_DATA')
        col.operator("wavetex.handoff", icon='TEXT')


CLASSES = (
    WaveTexProps, WT_LibraryItem, WT_UL_library,
    WT_OT_example, WT_OT_lib_save, WT_OT_lib_apply, WT_OT_lib_delete, WT_OT_lib_refresh,
    WT_OT_aura_seed_step, WT_OT_balance_zones, WT_OT_view_camera, WT_OT_film_stock,
    WT_OT_check_contrast,
    WT_OT_setup, WT_OT_sync, WT_OT_random_seed, WT_OT_seed_step, WT_OT_cycle_harmony,
    WT_OT_reverse_gradient, WT_OT_random_filter, WT_OT_reset_filter, WT_OT_surprise,
    WT_OT_preset, WT_OT_play_loop, WT_OT_stop, WT_OT_rewind,
    WT_OT_fx_preset, WT_OT_rebuild_patterns, WT_OT_set_size, WT_OT_export,
    WT_OT_iri_build, WT_OT_iri_bake_sim, WT_OT_iri_bake_lut, WT_OT_iri_preset, WT_OT_iri_random,
    WT_OT_poster, WT_OT_handoff,
    WT_PT_main, WT_PT_library, WT_PT_mypresets, WT_PT_presets, WT_PT_aura, WT_PT_wave, WT_PT_transform, WT_PT_color,
    WT_PT_filter, WT_PT_noise, WT_PT_overlay, WT_PT_surface,
    WT_PT_iri, WT_PT_iri_film, WT_PT_iri_streak, WT_PT_iri_sim, WT_PT_iri_substrate,
    WT_PT_fx, WT_PT_fx_dither, WT_PT_fx_blur, WT_PT_fx_lens, WT_PT_grain,
    WT_PT_legibility, WT_PT_contrast, WT_PT_export,
)


@bpy.app.handlers.persistent
@bpy.app.handlers.persistent
def _on_depsgraph(scene, _depsgraph=None):
    """Keep the baked patterns the same size as the render.

    Compositor Image nodes do not stretch to fit the frame - they sit at their
    own pixel size. Change the resolution in the Output panel and the grain,
    dither and scrim would cover only part of the frame as a hard-edged
    rectangle, which is exactly the kind of thing you only notice at export.
    """
    try:
        p = scene.wavetex
    except AttributeError:
        return
    if bpy.data.materials.get(MAT_NAME) is None:
        return
    r = scene.render
    ref = bpy.data.images.get("WT_Grain")
    if ref is None or r.resolution_x < 4 or r.resolution_y < 4:
        return
    if tuple(ref.size) == (r.resolution_x, r.resolution_y):
        return
    build_patterns(r.resolution_x, r.resolution_y, p.seed,
                   scrim=(p.scrim_dir, p.scrim_coverage, p.scrim_softness),
                   edge=(p.edge_inset, p.edge_softness),
                   grain=dict(size=p.grain_size, roughness=p.grain_roughness,
                              scale=p.grain_scale, chroma=p.grain_chroma))
    sync_fx(bpy.context)


def _on_load(_dummy):
    """Blender stores generated images by their settings, not their pixels, so
    the dither/grain patterns come back blank (or missing) after a reload.
    Regenerate them - they are deterministic, so the look is unchanged."""
    ctx = bpy.context
    sc = ctx.scene
    try:
        if sc.use_nodes and sc.node_tree and 'DitherPattern' in sc.node_tree.nodes:
            build_patterns(sc.render.resolution_x, sc.render.resolution_y)
            sync_fx(ctx)
        # pipeline B images are file-backed and do survive, but the nodes still
        # need re-pointing at the reloaded datablocks
        if bpy.data.materials.get(IRI_MAT):
            d = sim_dir()
            for nm, fn in (("WT_FilmLUT", "film_lut.exr"), ("WT_SimHeight", "height_0001.exr")):
                if bpy.data.images.get(nm) is None:
                    path = os.path.join(d, fn)
                    if os.path.exists(path):
                        img = bpy.data.images.load(path)
                        img.name = nm
                        img.colorspace_settings.name = 'Non-Color'
                        if nm == "WT_SimHeight":
                            img.source = 'SEQUENCE'
            sync_iri(ctx)
        library_refresh(sc)
    except Exception as exc:      # never block opening a file
        print("[wave_texture_maker] restore skipped:", exc)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.wavetex = bpy.props.PointerProperty(type=WaveTexProps)
    bpy.types.Scene.wavetex_library = bpy.props.CollectionProperty(type=WT_LibraryItem)
    bpy.types.Scene.wavetex_library_index = bpy.props.IntProperty(default=0)
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    # bpy.data is restricted during register(), so populate the list lazily
    try:
        for sc in bpy.data.scenes:
            library_refresh(sc)
    except Exception:
        pass


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    del bpy.types.Scene.wavetex_library_index
    del bpy.types.Scene.wavetex_library
    del bpy.types.Scene.wavetex
    for c in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass       # a reload can leave classes that were never registered


if __name__ == "__main__":
    register()
