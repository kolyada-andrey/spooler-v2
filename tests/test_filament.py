"""persistence.py — filament_mm_to_grams."""

import math

from persistence import FILAMENT_DENSITY, filament_mm_to_grams


def test_filament_mm_to_grams_zero_is_zero():
    assert filament_mm_to_grams(0) == 0


def test_filament_mm_to_grams_matches_manual_volume_calc():
    mm = 1000.0
    density = 1.24
    radius_cm = 0.175 / 2
    expected = round(math.pi * radius_cm ** 2 * (mm / 10) * density, 1)
    assert filament_mm_to_grams(mm, density) == expected


def test_filament_mm_to_grams_uses_default_density():
    assert filament_mm_to_grams(1000.0) == filament_mm_to_grams(1000.0, FILAMENT_DENSITY)


def test_filament_mm_to_grams_scales_with_density():
    radius_cm = 0.175 / 2
    vol_cm3 = math.pi * radius_cm ** 2 * (1000.0 / 10)
    assert filament_mm_to_grams(1000.0, 1.0) == round(vol_cm3 * 1.0, 1)
    assert filament_mm_to_grams(1000.0, 2.0) == round(vol_cm3 * 2.0, 1)
