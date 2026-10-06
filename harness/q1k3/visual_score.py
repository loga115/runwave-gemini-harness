"""Deterministic visual reward shared by each verifier and MCP harness.

The production reward is an equal additive combination of direct ImageHash
pHash and spatial edge layout. SIFT geometry is retained as a diagnostic for
future experiments, but is not part of the reward.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image as PILImage
from PIL import ImageChops, ImageFilter, ImageStat


SCORE_PROFILE = "phash-layout-v1"
PHASH_WEIGHT = 0.50
LAYOUT_WEIGHT = 0.50
SIFT_WEIGHT = 0.00
PHASH_HASH_SIZE = 8
PHASH_HIGHFREQ_FACTOR = 4
PHASH_BITS = PHASH_HASH_SIZE * PHASH_HASH_SIZE
CANNY_LOW = 60
CANNY_HIGH = 140
EDGE_DISTANCE_SIGMA = 4.0
LAYOUT_GRID = 4
ECC_DENSITY_PENALTY = 0.8
SIFT_FEATURES = 1500
SIFT_CONTRAST_THRESHOLD = 0.02
SIFT_RATIO = 0.75
SIFT_RANSAC_THRESHOLD = 5.0
SIFT_INLIER_BASELINE = 23.0
SIFT_INLIER_SATURATION = 200.0
SIFT_COVERAGE_BASELINE = 0.374
SIFT_COVERAGE_SATURATION = 1.0
PIXEL_RMS_SCALE = 24.0
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_777_216
SCORE_PARAMETERS = {
    "phash_weight": PHASH_WEIGHT,
    "layout_weight": LAYOUT_WEIGHT,
    "sift_weight": SIFT_WEIGHT,
    "phash_implementation": "imagehash.phash",
    "phash_hash_size": PHASH_HASH_SIZE,
    "phash_highfreq_factor": PHASH_HIGHFREQ_FACTOR,
    "phash_bits": PHASH_BITS,
    "canny_low": CANNY_LOW,
    "canny_high": CANNY_HIGH,
    "layout_grid": LAYOUT_GRID,
    "edge_distance_sigma": EDGE_DISTANCE_SIGMA,
    "ecc_density_penalty": ECC_DENSITY_PENALTY,
    "sift_features": SIFT_FEATURES,
    "sift_contrast_threshold": SIFT_CONTRAST_THRESHOLD,
    "sift_ratio": SIFT_RATIO,
    "sift_ransac_threshold": SIFT_RANSAC_THRESHOLD,
    "sift_inlier_baseline": SIFT_INLIER_BASELINE,
    "sift_inlier_saturation": SIFT_INLIER_SATURATION,
    "sift_coverage_baseline": SIFT_COVERAGE_BASELINE,
    "sift_coverage_saturation": SIFT_COVERAGE_SATURATION,
    "featureless_guard": "min(composite, pixel)",
}


@dataclass(frozen=True)
class VisualScore:
    """All metrics for one reference/candidate pair."""

    score: float
    phash: float
    layout_structure: float
    sift_geometry: float
    pixel: float
    ssim: float
    valid: bool = True


def _clamp(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _gray(path: Path) -> np.ndarray | None:
    """Decode a bounded PNG as an 8-bit grayscale OpenCV array."""

    try:
        if path.suffix.lower() != ".png" or not path.is_file():
            return None
        if path.stat().st_size > MAX_IMAGE_BYTES:
            return None
        with path.open("rb") as stream:
            header = stream.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            return None
        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    except (OSError, ValueError, cv2.error):
        return None
    if image is None or image.size == 0 or image.ndim != 2:
        return None
    decoded_height, decoded_width = image.shape
    return image if (decoded_width, decoded_height) == (width, height) else None


def _edge_map(gray: np.ndarray) -> np.ndarray:
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    return cv2.Canny(blurred, CANNY_LOW, CANNY_HIGH).astype(bool)


def _soft_edge_agreement(reference: np.ndarray, candidate: np.ndarray) -> float:
    height, width = reference.shape
    scores: list[float] = []
    for row in range(LAYOUT_GRID):
        for column in range(LAYOUT_GRID):
            reference_tile = reference[
                row * height // LAYOUT_GRID : (row + 1) * height // LAYOUT_GRID,
                column * width // LAYOUT_GRID : (column + 1) * width // LAYOUT_GRID,
            ]
            candidate_tile = candidate[
                row * height // LAYOUT_GRID : (row + 1) * height // LAYOUT_GRID,
                column * width // LAYOUT_GRID : (column + 1) * width // LAYOUT_GRID,
            ]
            if not reference_tile.any() and not candidate_tile.any():
                scores.append(1.0)
            elif not reference_tile.any() or not candidate_tile.any():
                scores.append(0.0)
            else:
                reference_distance = cv2.distanceTransform((~reference_tile).astype(np.uint8), cv2.DIST_L2, 3)
                candidate_distance = cv2.distanceTransform((~candidate_tile).astype(np.uint8), cv2.DIST_L2, 3)
                scores.append(
                    float(
                        (
                            np.exp(-candidate_distance[reference_tile] / EDGE_DISTANCE_SIGMA).mean()
                            + np.exp(-reference_distance[candidate_tile] / EDGE_DISTANCE_SIGMA).mean()
                        )
                        / 2.0
                    )
                )
    return _clamp(float(np.mean(scores))) if scores else 0.0


def _edge_density_agreement(reference: np.ndarray, candidate: np.ndarray) -> float:
    reference_density = float(reference.mean())
    candidate_density = float(candidate.mean())
    return _clamp(1.0 - abs(reference_density - candidate_density) / (reference_density + candidate_density + 1e-6))


def _translation_correlation(reference: np.ndarray, candidate: np.ndarray) -> float:
    reference_small = cv2.GaussianBlur(cv2.resize(reference, (256, 144)).astype(np.float32) / 255.0, (5, 5), 0)
    candidate_small = cv2.GaussianBlur(cv2.resize(candidate, (256, 144)).astype(np.float32) / 255.0, (5, 5), 0)
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        correlation, _ = cv2.findTransformECC(
            reference_small,
            candidate_small,
            warp,
            cv2.MOTION_TRANSLATION,
            (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-5),
            None,
            1,
        )
    except cv2.error:
        return 0.0
    return _clamp((float(correlation) + 1.0) / 2.0)


def _layout_score(reference: np.ndarray, candidate: np.ndarray) -> float:
    reference_edges = _edge_map(cv2.resize(reference, (320, 180)))
    candidate_edges = _edge_map(cv2.resize(candidate, (320, 180)))
    density = _edge_density_agreement(reference_edges, candidate_edges)
    registered = max(0.0, _translation_correlation(reference, candidate) - ECC_DENSITY_PENALTY * (1.0 - density))
    return _clamp(
        0.35 * _soft_edge_agreement(reference_edges, candidate_edges)
        + 0.35 * density
        + 0.30 * registered
    )


def _sift_score(reference: np.ndarray, candidate: np.ndarray) -> float:
    def describe(image: np.ndarray) -> tuple[tuple[int, int], list[cv2.KeyPoint], np.ndarray | None]:
        if image.shape[1] > 960:
            image = cv2.resize(image, (960, round(image.shape[0] * 960 / image.shape[1])))
        try:
            sift = cv2.SIFT_create(nfeatures=SIFT_FEATURES, contrastThreshold=SIFT_CONTRAST_THRESHOLD)
        except (AttributeError, cv2.error):
            return image.shape, [], None
        keypoints, descriptors = sift.detectAndCompute(image, None)
        return image.shape, keypoints, descriptors

    reference_shape, reference_points, reference_descriptors = describe(reference)
    _, candidate_points, candidate_descriptors = describe(candidate)
    if reference_descriptors is None or candidate_descriptors is None:
        return 0.0
    try:
        pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(reference_descriptors, candidate_descriptors, k=2)
        good = [first for first, second in pairs if first.distance < SIFT_RATIO * second.distance]
    except (ValueError, cv2.error):
        return 0.0
    if len(good) < 4:
        return 0.0
    source = np.float32([reference_points[item.queryIdx].pt for item in good])
    destination = np.float32([candidate_points[item.trainIdx].pt for item in good])
    try:
        _, mask = cv2.findHomography(source, destination, cv2.RANSAC, SIFT_RANSAC_THRESHOLD)
    except cv2.error:
        return 0.0
    if mask is None:
        return 0.0
    inlier_mask = mask.ravel().astype(bool)
    inliers = int(inlier_mask.sum())
    points = source[inlier_mask]
    hull_area = cv2.contourArea(cv2.convexHull(points)) if len(points) >= 3 else 0.0
    coverage = hull_area / float(reference_shape[0] * reference_shape[1])
    inlier_score = _clamp((inliers - SIFT_INLIER_BASELINE) / (SIFT_INLIER_SATURATION - SIFT_INLIER_BASELINE))
    coverage_score = _clamp((coverage - SIFT_COVERAGE_BASELINE) / (SIFT_COVERAGE_SATURATION - SIFT_COVERAGE_BASELINE))
    return _clamp(0.7 * inlier_score + 0.3 * coverage_score)


def _phash_score(reference: Path, candidate: Path) -> float:
    """Return direct ``imagehash.phash`` Hamming similarity in ``[0, 1]``."""

    try:
        with PILImage.open(reference) as reference_image, PILImage.open(candidate) as candidate_image:
            reference_hash = imagehash.phash(reference_image, hash_size=PHASH_HASH_SIZE, highfreq_factor=PHASH_HIGHFREQ_FACTOR)
            candidate_hash = imagehash.phash(candidate_image, hash_size=PHASH_HASH_SIZE, highfreq_factor=PHASH_HIGHFREQ_FACTOR)
        return _clamp(1.0 - int(reference_hash - candidate_hash) / float(PHASH_BITS))
    except (OSError, ValueError, TypeError):
        return 0.0


def _pixel_similarity(reference: Path, candidate: Path) -> float:
    """Retain RGB similarity as a diagnostic and flat-image guard."""

    try:
        with PILImage.open(reference) as reference_image, PILImage.open(candidate) as candidate_image:
            reference_rgb = reference_image.convert("RGB")
            candidate_rgb = candidate_image.convert("RGB")
            if reference_rgb.size != candidate_rgb.size:
                return 0.0
            if reference_rgb.tobytes() == candidate_rgb.tobytes():
                return 1.0
            rms = ImageStat.Stat(ImageChops.difference(reference_rgb, candidate_rgb)).rms
            mse = sum(channel_rms**2 for channel_rms in rms) / 3.0
            return _clamp(math.exp(-mse / (2.0 * PIXEL_RMS_SCALE**2)))
    except (OSError, ValueError, ZeroDivisionError):
        return 0.0


def _ssim_similarity(reference: Path, candidate: Path) -> float:
    """Compute the bounded structural diagnostic retained for reporting."""

    try:
        with PILImage.open(reference) as reference_image, PILImage.open(candidate) as candidate_image:
            if reference_image.size != candidate_image.size:
                return 0.0
            reference_gray = reference_image.convert("L")
            candidate_gray = candidate_image.convert("L")
            if reference_gray.tobytes() == candidate_gray.tobytes():
                return 1.0
            width, height = reference_gray.size
            sample_width = min(320, width)
            sample_height = max(1, round(height * sample_width / width))
            reference_small = reference_gray.resize((sample_width, sample_height), PILImage.Resampling.BILINEAR)
            candidate_small = candidate_gray.resize((sample_width, sample_height), PILImage.Resampling.BILINEAR)
            blur_reference = reference_small.filter(ImageFilter.GaussianBlur(1.5))
            blur_candidate = candidate_small.filter(ImageFilter.GaussianBlur(1.5))
            square_reference = reference_small.point(lambda value: (value * value + 127) // 255)
            square_candidate = candidate_small.point(lambda value: (value * value + 127) // 255)
            product = ImageChops.multiply(reference_small, candidate_small)
            blur_square_reference = square_reference.filter(ImageFilter.GaussianBlur(1.5))
            blur_square_candidate = square_candidate.filter(ImageFilter.GaussianBlur(1.5))
            blur_product = product.filter(ImageFilter.GaussianBlur(1.5))
            c1 = (0.01 * 255) ** 2
            c2 = (0.03 * 255) ** 2
            values: list[float] = []
            for mean_reference, mean_candidate, second_reference, second_candidate, cross in zip(
                blur_reference.getdata(), blur_candidate.getdata(), blur_square_reference.getdata(), blur_square_candidate.getdata(), blur_product.getdata()
            ):
                variance_reference = max(0.0, second_reference - mean_reference * mean_reference / 255.0)
                variance_candidate = max(0.0, second_candidate - mean_candidate * mean_candidate / 255.0)
                covariance = cross - mean_reference * mean_candidate / 255.0
                numerator = (2.0 * mean_reference * mean_candidate + c1) * (2.0 * covariance + c2)
                denominator = (mean_reference * mean_reference + mean_candidate * mean_candidate + c1) * (variance_reference + variance_candidate + c2)
                values.append(numerator / denominator if denominator else 1.0)
            return _clamp(sum(values) / len(values)) if values else 0.0
    except (OSError, ValueError, ZeroDivisionError):
        return 0.0


def score_pair(reference: Path, candidate: Path) -> VisualScore:
    """Return the additive production reward for one image pair."""

    reference_gray = _gray(reference)
    candidate_gray = _gray(candidate)
    if reference_gray is None or candidate_gray is None or reference_gray.shape != candidate_gray.shape:
        return VisualScore(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, valid=False)
    pixel = _pixel_similarity(reference, candidate)
    structural = _ssim_similarity(reference, candidate)
    if np.array_equal(reference_gray, candidate_gray):
        return VisualScore(1.0, 1.0, 1.0, 1.0, pixel, structural)
    hash_score = _phash_score(reference, candidate)
    layout_score = _layout_score(reference_gray, candidate_gray)
    sift_score = _sift_score(reference_gray, candidate_gray)
    combined = _clamp(PHASH_WEIGHT * hash_score + LAYOUT_WEIGHT * layout_score + SIFT_WEIGHT * sift_score)
    reference_edges = _edge_map(cv2.resize(reference_gray, (320, 180)))
    candidate_edges = _edge_map(cv2.resize(candidate_gray, (320, 180)))
    if not reference_edges.any() or not candidate_edges.any():
        combined = min(combined, pixel)
    return VisualScore(combined, hash_score, layout_score, sift_score, pixel, structural)
