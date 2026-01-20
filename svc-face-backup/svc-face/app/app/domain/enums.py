# services/svc-face/app/app/domain/enums.py
from __future__ import annotations
from enum import Enum

# ONLY fixed enums - everything else comes from database

class FaceGenerationMode(str, Enum):
    TEXT_TO_IMAGE = "text-to-image"
    IMAGE_TO_IMAGE = "image-to-image"

class Gender(str, Enum):
    MALE = "male"
    FEMALE = "female"
    NEUTRAL = "neutral"

class SupportedLanguage(str, Enum):
    ENGLISH = "en"
    HINDI = "hi"
    TAMIL = "ta"
    TELUGU = "te"
    KANNADA = "kn"
    MALAYALAM = "ml"
    BENGALI = "bn"
    MARATHI = "mr"
    GUJARATI = "gu"
    PUNJABI = "pa"

# NOTE: Regions, age_groups, styles, contexts are fetched from database
# See: face_generation_regions, face_generation_contexts tables
