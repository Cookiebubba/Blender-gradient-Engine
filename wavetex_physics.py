"""Physics core for the Wave Texture Maker's iridescence pipeline.

Two genuinely physical pieces live here:

1. simulate_loop()  - a spectral (Tessendorf-style) gravity-capillary wave
   solver. Each Fourier mode is evolved with the real dispersion relation
   omega^2 = g*k + (sigma/rho)*k^3. Perfect looping comes from snapping every
   omega to an integer multiple of the loop frequency, so after T seconds every
   mode has completed a whole number of cycles and the field is bit-identical.

2. thin_film_lut() - reflectance of a thin film via the Airy summation over
   Fresnel coefficients, integrated against the CIE 1931 colour matching
   functions and converted to sRGB. This is what actually produces the rainbow:
   colour as a function of film thickness, not an artistic gradient.
"""

import numpy as np

G = 9.81                # m/s^2
SIGMA_OVER_RHO = 7.4e-5  # water surface tension / density, m^3/s^2


# --------------------------------------------------------------- wave sim ---

def simulate_loop(n=256, frames=120, domain=0.35, wind_speed=2.4,
                  wind_dir=(1.0, 0.45), capillary=1.0, choppiness=0.0,
                  seed=7, duration=4.0, times=None):
    """Return (frames, n, n) float32 height field that loops exactly.

    domain      - physical size of the tile in metres (small => capillary ripples)
    duration    - seconds the loop spans; also the period every mode is snapped to
    times       - explicit sample times instead of the uniform grid. Sampling
                  t=duration must reproduce t=0; that is the loop-closure test.
    """
    rng = np.random.default_rng(seed)

    # wavevector grid for a periodic tile of side `domain`
    idx = np.fft.fftfreq(n, d=1.0 / n)
    kx, ky = np.meshgrid(2.0 * np.pi * idx / domain, 2.0 * np.pi * idx / domain, indexing='xy')
    k = np.hypot(kx, ky)
    k_safe = np.where(k == 0, 1e-6, k)

    # Phillips spectrum, directionally biased by the wind
    w = np.array(wind_dir, dtype=np.float64)
    w /= np.linalg.norm(w)
    L_w = wind_speed ** 2 / G
    k_hat_x, k_hat_y = kx / k_safe, ky / k_safe
    cos_term = (k_hat_x * w[0] + k_hat_y * w[1]) ** 2
    damp = np.exp(-k_safe ** 2 * (domain / n) ** 2)      # kill sub-cell noise
    phillips = np.exp(-1.0 / (k_safe * L_w) ** 2) / k_safe ** 4 * cos_term * damp
    phillips[k == 0] = 0.0
    phillips = np.clip(phillips, 0.0, None)

    # complex Gaussian amplitudes
    xi = rng.normal(size=(n, n)) + 1j * rng.normal(size=(n, n))
    h0 = xi * np.sqrt(phillips / 2.0)
    h0_conj = np.conj(np.roll(np.roll(h0[::-1, ::-1], 1, axis=0), 1, axis=1))

    # real dispersion, then quantised so every mode closes the loop
    omega = np.sqrt(G * k_safe + capillary * SIGMA_OVER_RHO * k_safe ** 3)
    omega_0 = 2.0 * np.pi / duration
    omega_q = np.round(omega / omega_0) * omega_0
    omega_q[k == 0] = 0.0

    if times is None:
        ts = np.arange(frames) * duration / frames
    else:
        ts = np.asarray(times, dtype=np.float64)
        frames = ts.size

    out = np.empty((frames, n, n), dtype=np.float32)
    dx = dy = None
    if choppiness > 0.0:
        dx = np.empty_like(out)
        dy = np.empty_like(out)

    for f, t in enumerate(ts):
        phase = np.exp(1j * omega_q * t)
        hk = h0 * phase + h0_conj * np.conj(phase)
        out[f] = np.real(np.fft.ifft2(hk)) * (n * n)
        if choppiness > 0.0:
            dxk = -1j * (kx / k_safe) * hk
            dyk = -1j * (ky / k_safe) * hk
            dx[f] = np.real(np.fft.ifft2(dxk)) * (n * n)
            dy[f] = np.real(np.fft.ifft2(dyk)) * (n * n)

    # normalise to 0..1 across the whole loop so baked frames stay consistent
    lo, hi = out.min(), out.max()
    if hi - lo < 1e-12:
        out[:] = 0.5
    else:
        out = (out - lo) / (hi - lo)
    return out.astype(np.float32)


# ------------------------------------------------------------- thin film ---

def _cie_xyz(lam):
    """Wyman/Sloan/Shirley multi-lobe fits to the CIE 1931 2-deg observer."""
    def g(x, mu, s1, s2):
        s = np.where(x < mu, s1, s2)
        return np.exp(-0.5 * ((x - mu) / s) ** 2)
    x = 1.056 * g(lam, 599.8, 37.9, 31.0) + 0.362 * g(lam, 442.0, 16.0, 26.7) \
        - 0.065 * g(lam, 501.1, 20.4, 26.2)
    y = 0.821 * g(lam, 568.8, 46.9, 40.5) + 0.286 * g(lam, 530.9, 16.3, 31.1)
    z = 1.217 * g(lam, 437.0, 11.8, 36.0) + 0.681 * g(lam, 459.0, 26.0, 13.8)
    return x, y, z


def _fresnel(n_i, n_t, cos_i):
    """Amplitude reflection coefficients (s and p) at one interface."""
    sin_t2 = (n_i / n_t) ** 2 * (1.0 - cos_i ** 2)
    cos_t = np.sqrt(np.clip(1.0 - sin_t2, 0.0, 1.0))
    rs = (n_i * cos_i - n_t * cos_t) / (n_i * cos_i + n_t * cos_t)
    rp = (n_t * cos_i - n_i * cos_t) / (n_t * cos_i + n_i * cos_t)
    return rs, rp, cos_t


def thin_film_lut(samples=512, d_min=0.0, d_max=1400.0, n_film=1.35,
                  n_sub=1.0, angle_deg=0.0, n_air=1.0):
    """RGB as a function of film thickness (nm) - the Airy reflectance of a
    single film, spectrally integrated. Returns (samples, 3) in linear sRGB."""
    lam = np.linspace(390.0, 750.0, 96)                 # nm
    d = np.linspace(d_min, d_max, samples)[:, None]     # nm

    cos_i = np.cos(np.radians(angle_deg))
    r01s, r01p, cos_f = _fresnel(n_air, n_film, cos_i)
    r12s, r12p, _ = _fresnel(n_film, n_sub, cos_f)

    # phase thickness of the film for each wavelength
    beta = 2.0 * np.pi * n_film * d * cos_f / lam[None, :]
    e = np.exp(-2j * beta)

    R = np.zeros((samples, lam.size))
    for r01, r12 in ((r01s, r12s), (r01p, r12p)):
        r = (r01 + r12 * e) / (1.0 + r01 * r12 * e)
        R += np.abs(r) ** 2
    R *= 0.5                                            # unpolarised average

    integrate = getattr(np, "trapezoid", None) or np.trapz
    xb, yb, zb = _cie_xyz(lam)
    norm = integrate(yb, lam)
    X = integrate(R * xb[None, :], lam, axis=1) / norm
    Y = integrate(R * yb[None, :], lam, axis=1) / norm
    Z = integrate(R * zb[None, :], lam, axis=1) / norm

    M = np.array([[3.2406, -1.5372, -0.4986],
                  [-0.9689, 1.8758, 0.0415],
                  [0.0557, -0.2040, 1.0570]])
    rgb = np.stack([X, Y, Z], axis=1) @ M.T
    rgb = np.clip(rgb, 0.0, None)
    peak = rgb.max()
    if peak > 0:
        rgb /= peak
    return rgb.astype(np.float32)
