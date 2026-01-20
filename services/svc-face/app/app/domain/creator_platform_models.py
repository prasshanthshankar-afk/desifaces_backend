# domain/creator_platform_models.py
# Database table models for creator platform - ZERO BUGS

from __future__ import annotations
from typing import Dict, List, Any, Optional
from pydantic import BaseModel
from datetime import datetime

# ============================================================================
# CREATOR PLATFORM DATABASE MODELS - EXACT MAPPING TO YOUR TABLES
# ============================================================================

class ImageFormatDB(BaseModel):
    """Maps EXACTLY to face_generation_image_formats table schema"""
    id: str                           # UUID -> str
    code: str                         # TEXT UNIQUE NOT NULL
    display_name: Dict[str, str]      # JSONB -> dict ({"en": "...", "hi": "..."})
    width: int                        # INTEGER NOT NULL
    height: int                       # INTEGER NOT NULL
    aspect_ratio: str                 # TEXT NOT NULL
    platform_category: str           # TEXT NOT NULL
    recommended_platforms: List[str]  # TEXT[] -> list (converted by repository)
    technical_specs: Dict[str, Any]   # JSONB -> dict
    safe_zones: Dict[str, Any]        # JSONB -> dict
    is_active: bool = True           # BOOLEAN DEFAULT true
    sort_order: int = 0              # INTEGER DEFAULT 0
    created_at: datetime             # TIMESTAMPTZ

class UseCaseDB(BaseModel):
    """Maps EXACTLY to face_generation_use_cases table schema"""
    id: str                               # UUID -> str
    code: str                             # TEXT UNIQUE NOT NULL
    display_name: Dict[str, str]          # JSONB -> dict
    category: str                         # TEXT NOT NULL
    description: Optional[Dict[str, str]] # JSONB -> dict
    prompt_base: str                      # TEXT NOT NULL
    lighting_style: Optional[str]         # TEXT
    composition_style: Optional[str]      # TEXT
    mood_descriptors: Optional[str]       # TEXT
    background_type: Optional[str]        # TEXT
    recommended_formats: List[str]        # TEXT[] -> list
    target_audience: Optional[str]        # TEXT
    industry_focus: List[str]             # TEXT[] -> list
    is_active: bool = True               # BOOLEAN DEFAULT true
    sort_order: int = 0                  # INTEGER DEFAULT 0
    created_at: datetime                 # TIMESTAMPTZ

class AgeRangeDB(BaseModel):
    """Maps EXACTLY to face_generation_age_ranges table schema"""
    id: str                           # UUID -> str
    code: str                         # TEXT UNIQUE NOT NULL
    display_name: Dict[str, str]      # JSONB -> dict
    min_age: int                      # INTEGER NOT NULL
    max_age: int                      # INTEGER NOT NULL
    prompt_descriptor: str            # TEXT NOT NULL
    professional_contexts: List[str]  # TEXT[] -> list
    is_active: bool = True           # BOOLEAN DEFAULT true

class RegionDB(BaseModel):
    """Maps EXACTLY to face_generation_regions table schema"""
    id: str                           # UUID -> str
    code: str                         # TEXT UNIQUE NOT NULL
    display_name: Dict[str, str]      # JSONB -> dict
    sub_region: Optional[str]         # TEXT
    ethnicity_notes: Optional[str]    # TEXT
    typical_skin_tones: List[str]     # TEXT[] -> list
    prompt_base: str                  # TEXT NOT NULL
    is_active: bool = True           # BOOLEAN DEFAULT true
    sort_order: int = 0              # INTEGER DEFAULT 0
    created_at: datetime             # TIMESTAMPTZ

class SkinToneDB(BaseModel):
    """Maps EXACTLY to face_generation_skin_tones table schema"""
    id: str                          # UUID -> str
    code: str                        # TEXT UNIQUE NOT NULL
    display_name: Dict[str, str]     # JSONB -> dict
    hex_reference: Optional[str]     # TEXT
    prompt_descriptor: str           # TEXT NOT NULL
    diversity_weight: int = 1       # INTEGER DEFAULT 1
    is_active: bool = True          # BOOLEAN DEFAULT true