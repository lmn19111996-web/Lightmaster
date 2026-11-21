import numpy as np
import pytest

from lightsync.compositor import Encoder, mix

N = 4
TINT = (1.0, 0.82, 0.67)


def test_no_white_passes_color_through():
    C = np.full((N, 3), 0.2, np.float32)
    assert np.allclose(mix(C, np.zeros(N), TINT, duck=0.4), C)


def test_full_white_with_full_duck_is_pure_tint():
    C = np.full((N, 3), 0.5, np.float32)
    assert np.allclose(mix(C, np.ones(N), TINT, duck=1.0), TINT)


def test_overflow_clip_vs_normalize():
    C = np.tile([[0.0, 0.0, 1.0]], (N, 1)).astype(np.float32)
    w = np.ones(N)
    clipped = mix(C, w, (1, 1, 1), duck=0.0, overflow="clip")
    normed = mix(C, w, (1, 1, 1), duck=0.0, overflow="normalize")
    assert np.allclose(clipped, 1.0)                    # burns out to pure white
    assert np.allclose(normed[0], [0.5, 0.5, 1.0])      # keeps the blue tint
    with pytest.raises(ValueError):
        mix(C, w, TINT, 0.0, overflow="bogus")


def test_encode_endpoints():
    e = Encoder(N)
    assert (e.encode(np.zeros((N, 3))) == 0).all()
    assert (e.encode(np.ones((N, 3))) == 255).all()
    assert (e.encode(np.full((N, 3), 2.0)) == 255).all()   # out of range is clipped


def test_dither_average_matches_fractional_level():
    e = Encoder(1)
    L = np.full((1, 3), (10.3 / 255) ** 2.2)                 # encodes to 10.3
    frames = np.array([e.encode(L) for _ in range(1000)], dtype=float)
    assert abs(frames.mean() - 10.3) < 0.01
    assert set(np.unique(frames)) <= {10.0, 11.0}


def test_black_after_dim_stays_black():
    e = Encoder(1)
    e.encode(np.full((1, 3), 1e-4))
    assert (e.encode(np.zeros((1, 3))) == 0).all()
