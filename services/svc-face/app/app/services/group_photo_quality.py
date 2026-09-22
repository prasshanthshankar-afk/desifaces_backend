from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from desifaces_shared.safety import SafetyStatus


@dataclass(frozen=True)
class GroupPhotoQualityCheck:
    code: str
    status: SafetyStatus
    title: str
    reason: str
    required_action: str = ""
    subject: str | None = None
    retryable: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status.value,
            "title": self.title,
            "reason": self.reason,
            "required_action": self.required_action,
            "subject": self.subject,
            "retryable": self.retryable,
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class GroupPhotoQualityDecision:
    status: SafetyStatus
    summary: str
    checks: List[GroupPhotoQualityCheck]
    detected_faces: List[Dict[str, Any]]
    image_width: int
    image_height: int
    expected_speakers: int
    contract_version: int = 1

    @property
    def usable(self) -> bool:
        return self.status != SafetyStatus.FAIL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "usable": self.usable,
            "status": self.status.value,
            "summary": self.summary,
            "contract_version": self.contract_version,
            "expected_speakers": self.expected_speakers,
            "detected_face_count": len(self.detected_faces),
            "image_width": self.image_width,
            "image_height": self.image_height,
            "faces": list(self.detected_faces),
            "checks": [check.to_dict() for check in self.checks],
        }


def _overall(checks: List[GroupPhotoQualityCheck]) -> SafetyStatus:
    if any(check.status == SafetyStatus.FAIL for check in checks):
        return SafetyStatus.FAIL
    if any(check.status == SafetyStatus.WARN for check in checks):
        return SafetyStatus.WARN
    return SafetyStatus.PASS


def _summary(status: SafetyStatus, checks: List[GroupPhotoQualityCheck]) -> str:
    failed = sum(1 for check in checks if check.status == SafetyStatus.FAIL)
    warned = sum(1 for check in checks if check.status == SafetyStatus.WARN)
    if status == SafetyStatus.FAIL:
        noun = "issue" if failed == 1 else "issues"
        return f"This photo cannot be used for the conversation yet. {failed} {noun} need attention."
    if status == SafetyStatus.WARN:
        noun = "warning" if warned == 1 else "warnings"
        return f"This photo can continue after review. {warned} {noun} should be checked."
    return "Photo quality checks passed for group conversation use."


def analyze_group_photo(image_bytes: bytes, *, expected_speakers: int) -> GroupPhotoQualityDecision:
    if expected_speakers < 2:
        raise ValueError("expected_speakers_must_be_at_least_two")

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:
        raise RuntimeError("group_photo_quality_dependencies_unavailable") from exc

    encoded = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("group_photo_image_unreadable")

    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    checks: List[GroupPhotoQualityCheck] = []

    if width < 960 or height < 540:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_RESOLUTION",
                status=SafetyStatus.FAIL,
                title="Image resolution is too low",
                reason=f"The uploaded photo is {width}×{height}. Reliable multi-person lip-sync needs more facial detail.",
                required_action="Use a larger image. For landscape group conversations, 1280×720 or higher is recommended.",
                metadata={"width": width, "height": height, "recommended": "1280x720+"},
            )
        )
    elif width < 1280 or height < 720:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_RESOLUTION",
                status=SafetyStatus.WARN,
                title="Image resolution is usable but below the recommendation",
                reason=f"The uploaded photo is {width}×{height}. It may provide less facial detail than a 1280×720 or larger image.",
                required_action="For better lip-sync quality, use a 1280×720 or larger photo when available.",
                metadata={"width": width, "height": height, "recommended": "1280x720+"},
            )
        )
    else:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_RESOLUTION",
                status=SafetyStatus.PASS,
                title="Image resolution is sufficient",
                reason=f"The uploaded photo is {width}×{height}, which provides suitable detail for this quality gate.",
                metadata={"width": width, "height": height},
            )
        )

    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if blur_score < 45.0:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_SHARPNESS",
                status=SafetyStatus.FAIL,
                title="The photo is too blurry",
                reason=f"The image sharpness score is {blur_score:.1f}, below the minimum required for reliable face and mouth animation.",
                required_action="Use a sharper photo with the speakers in focus and without motion blur.",
                metadata={"sharpness_score": round(blur_score, 2), "minimum": 45.0, "recommended": 90.0},
            )
        )
    elif blur_score < 90.0:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_SHARPNESS",
                status=SafetyStatus.WARN,
                title="The photo is slightly soft",
                reason=f"The image sharpness score is {blur_score:.1f}. Faces are detectable, but a sharper image may animate better.",
                required_action="Use a sharper photo if available, especially if a speaker's mouth or eyes look soft.",
                metadata={"sharpness_score": round(blur_score, 2), "recommended": 90.0},
            )
        )
    else:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_SHARPNESS",
                status=SafetyStatus.PASS,
                title="Image sharpness is sufficient",
                reason=f"The image sharpness score is {blur_score:.1f}.",
                metadata={"sharpness_score": round(blur_score, 2)},
            )
        )

    brightness = float(gray.mean())
    if brightness < 35.0:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_EXPOSURE",
                status=SafetyStatus.FAIL,
                title="The photo is too dark",
                reason=f"The average brightness is {brightness:.1f}; facial features may not be visible enough for reliable speaker animation.",
                required_action="Use a better-lit photo where every speaker's eyes, nose, mouth, and jawline are clearly visible.",
                metadata={"brightness": round(brightness, 2)},
            )
        )
    elif brightness > 225.0:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_EXPOSURE",
                status=SafetyStatus.FAIL,
                title="The photo is overexposed",
                reason=f"The average brightness is {brightness:.1f}; important facial detail may be washed out.",
                required_action="Use a photo with more balanced lighting and visible facial detail.",
                metadata={"brightness": round(brightness, 2)},
            )
        )
    elif brightness < 55.0 or brightness > 205.0:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_EXPOSURE",
                status=SafetyStatus.WARN,
                title="Lighting may reduce animation quality",
                reason=f"The average brightness is {brightness:.1f}, near the edge of the preferred exposure range.",
                required_action="Use a more evenly lit image if faces appear very dark or washed out.",
                metadata={"brightness": round(brightness, 2)},
            )
        )
    else:
        checks.append(
            GroupPhotoQualityCheck(
                code="IMAGE_EXPOSURE",
                status=SafetyStatus.PASS,
                title="Lighting is suitable",
                reason="The image exposure is within the preferred range for facial detail.",
                metadata={"brightness": round(brightness, 2)},
            )
        )

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        raise RuntimeError("group_photo_face_detector_unavailable")

    min_side = max(40, int(min(width, height) * 0.06))
    detections = detector.detectMultiScale(
        gray,
        scaleFactor=1.08,
        minNeighbors=5,
        minSize=(min_side, min_side),
    )

    faces: List[Dict[str, Any]] = []
    for index, (x, y, w, h) in enumerate(sorted(detections, key=lambda box: box[0]), start=1):
        faces.append(
            {
                "face_id": f"face_{index}",
                "box": {
                    "x": round(float(x) / float(width), 6),
                    "y": round(float(y) / float(height), 6),
                    "width": round(float(w) / float(width), 6),
                    "height": round(float(h) / float(height), 6),
                },
                "pixel_box": {"x": int(x), "y": int(y), "width": int(w), "height": int(h)},
                "height_ratio": round(float(h) / float(height), 6),
                "area_ratio": round(float(w * h) / float(width * height), 6),
            }
        )

    detected = len(faces)
    if detected < expected_speakers:
        checks.append(
            GroupPhotoQualityCheck(
                code="FACE_COUNT",
                status=SafetyStatus.FAIL,
                title="Not all speakers have a clearly detectable face",
                reason=f"The conversation has {expected_speakers} speakers, but only {detected} clear frontal face{' was' if detected == 1 else 's were'} detected.",
                required_action="Use a photo where every speaker's face is clearly visible, separated from other faces, and oriented toward the camera.",
                metadata={"expected_speakers": expected_speakers, "detected_faces": detected},
            )
        )
    elif detected > expected_speakers:
        checks.append(
            GroupPhotoQualityCheck(
                code="FACE_COUNT",
                status=SafetyStatus.WARN,
                title="Extra people are visible in the photo",
                reason=f"The conversation has {expected_speakers} speakers, but {detected} faces were detected.",
                required_action="During speaker mapping, select only the people who participate in the conversation. Use a simpler photo if the extra faces make mapping ambiguous.",
                metadata={"expected_speakers": expected_speakers, "detected_faces": detected},
            )
        )
    else:
        checks.append(
            GroupPhotoQualityCheck(
                code="FACE_COUNT",
                status=SafetyStatus.PASS,
                title="Speaker count matches the photo",
                reason=f"{detected} clearly detectable faces were found for {expected_speakers} speakers.",
                metadata={"expected_speakers": expected_speakers, "detected_faces": detected},
            )
        )

    candidate_faces = sorted(faces, key=lambda item: item["area_ratio"], reverse=True)[:expected_speakers]
    too_small = [face for face in candidate_faces if float(face["height_ratio"]) < 0.12]
    if detected >= expected_speakers and too_small:
        ids = ", ".join(face["face_id"] for face in too_small)
        checks.append(
            GroupPhotoQualityCheck(
                code="FACE_SIZE",
                status=SafetyStatus.FAIL,
                title="One or more speaker faces are too small",
                reason=f"{ids} do not occupy enough of the image height for reliable lip-sync.",
                required_action="Use a closer group photo or crop the image so every speaker's face is larger while keeping all speakers visible.",
                metadata={"minimum_face_height_ratio": 0.12, "affected_faces": [face["face_id"] for face in too_small]},
            )
        )
    elif detected >= expected_speakers:
        checks.append(
            GroupPhotoQualityCheck(
                code="FACE_SIZE",
                status=SafetyStatus.PASS,
                title="Speaker faces are large enough",
                reason="The detected speaker faces meet the minimum size needed by this quality gate.",
                metadata={"minimum_face_height_ratio": 0.12},
            )
        )

    near_edge: List[str] = []
    for face in candidate_faces:
        box = face["box"]
        if (
            float(box["x"]) < 0.01
            or float(box["y"]) < 0.01
            or float(box["x"]) + float(box["width"]) > 0.99
            or float(box["y"]) + float(box["height"]) > 0.99
        ):
            near_edge.append(str(face["face_id"]))
    if near_edge:
        checks.append(
            GroupPhotoQualityCheck(
                code="FACE_CROP",
                status=SafetyStatus.WARN,
                title="A face is very close to the image edge",
                reason=f"{', '.join(near_edge)} may be partially cropped or have limited room for natural motion.",
                required_action="Use a photo with a little more space around each speaker's head when possible.",
                metadata={"affected_faces": near_edge},
            )
        )

    checks.append(
        GroupPhotoQualityCheck(
            code="MOUTH_VISIBILITY_CONFIRMATION",
            status=SafetyStatus.WARN,
            title="Confirm each speaker's mouth is unobstructed",
            reason="Automated frontal-face detection cannot reliably prove that a hand, microphone, mask, hair, or another object is not covering the mouth.",
            required_action="During speaker mapping, visually confirm that each speaker's full mouth and lower face are visible. Choose another photo if any mouth is covered.",
            retryable=True,
        )
    )

    status = _overall(checks)
    return GroupPhotoQualityDecision(
        status=status,
        summary=_summary(status, checks),
        checks=checks,
        detected_faces=faces,
        image_width=width,
        image_height=height,
        expected_speakers=expected_speakers,
    )
