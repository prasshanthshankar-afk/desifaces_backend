from __future__ import annotations

from app.services.visual_profiles import (
    CommunityVisualProfile,
    VisualProfileRegistration,
)


INDIA_PROFILE = CommunityVisualProfile(
    profile_id="community.india.premium_human",
    version="1.0",
    t2i_demographic_fragments=(
        (
            "Indian cultural context with naturally diverse facial appearance; "
            "preserve natural variation in skin tones and facial features; use region "
            "to guide cultural context rather than infer or exaggerate facial anatomy; "
            "avoid genericized, homogenized or stereotyped appearance"
        ),
    ),
    t2i_quality_fragments=(
        (
            "culturally credible attire, grooming and adornment consistent with "
            "the supplied region, clothing, context, tradition and occasion; "
            "use bindi, jewelry or ceremonial details only when contextually appropriate"
        ),
        (
            "when Indian garments or accessories are requested, render physically "
            "credible fabric weave, drape, pleats, folds, borders, blouse or kurta "
            "construction, jewelry placement and material behavior"
        ),
    ),
    i2i_quality_fragments=(
        (
            "when wardrobe, jewelry or cultural styling is edited, keep it "
            "regionally coherent and physically believable while preserving the "
            "source person's exact identity"
        ),
    ),
    negative_fragments=(
        "stereotyped or caricatured Indian appearance",
        "generic festival costume, mismatched regional attire",
        "implausible garment drape, floating jewelry, fused jewelry",
    ),
)


INDIA_PROFILE_REGISTRATION = VisualProfileRegistration(
    profile=INDIA_PROFILE,
    country_codes=frozenset(
        {
            "IN",
            "IND",
            "INDIA",
        }
    ),
    region_codes=frozenset(
        {
            "AP",
            "ANDHRA_PRADESH",
            "AR",
            "ARUNACHAL_PRADESH",
            "AS",
            "ASSAM",
            "BR",
            "BIHAR",
            "CG",
            "CHHATTISGARH",
            "DL",
            "DELHI",
            "GA",
            "GOA",
            "GJ",
            "GUJARAT",
            "HR",
            "HARYANA",
            "HP",
            "HIMACHAL_PRADESH",
            "JH",
            "JHARKHAND",
            "KA",
            "KARNATAKA",
            "KL",
            "KERALA",
            "LA",
            "LADAKH",
            "MH",
            "MAHARASHTRA",
            "ML",
            "MEGHALAYA",
            "MN",
            "MANIPUR",
            "MP",
            "MADHYA_PRADESH",
            "MZ",
            "MIZORAM",
            "NL",
            "NAGALAND",
            "OD",
            "OR",
            "ODISHA",
            "PB",
            "PUNJAB",
            "PY",
            "PUDUCHERRY",
            "RJ",
            "RAJASTHAN",
            "SK",
            "SIKKIM",
            "TG",
            "TS",
            "TELANGANA",
            "TN",
            "TAMIL_NADU",
            "TR",
            "TRIPURA",
            "UK",
            "UT",
            "UTTARAKHAND",
            "UP",
            "UTTAR_PRADESH",
            "WB",
            "WEST_BENGAL",
        }
    ),
)
