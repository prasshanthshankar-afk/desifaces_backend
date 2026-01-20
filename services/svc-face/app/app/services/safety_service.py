# services/svc-face/app/app/services/safety_service.py
from __future__ import annotations
import re
from typing import Tuple
from azure.ai.contentsafety import ContentSafetyClient
from azure.core.credentials import AzureKeyCredential
from azure.ai.contentsafety.models import AnalyzeTextOptions, AnalyzeImageOptions, ImageData
from app.config import settings
import base64

# Blocked keywords (first line of defense)
BLOCKED_KEYWORDS = [
    "nude", "naked", "nsfw", "explicit", "sexual", "pornographic", "obscene",
    "lingerie", "bikini", "revealing", "transparent", "see-through", "exposed",
    "violence", "blood", "gore", "weapon", "kill", "fight", "abuse",
    "political", "election", "modi", "gandhi", "rahul", "bjp", "congress",
    "child abuse", "underage", "minor", "kid",
    "drugs", "cocaine", "heroin", "meth"
]

# Safety negative prompt (applied to ALL generations)
SAFETY_NEGATIVE_PROMPT = """
nude, nudity, naked, nsfw, explicit, sexual, pornographic, obscene, 
inappropriate, adult content, revealing clothing, transparent clothing, 
see-through, exposed body parts, sexual acts, suggestive poses,
violence, blood, gore, weapons, fighting, abuse, 
child in inappropriate context, underage, 
political symbols, political figures, controversial symbols,
hate symbols, offensive gestures, drugs, smoking, alcohol abuse,
ugly, distorted, deformed, extra limbs, bad anatomy,
low quality, blurry, watermark, text overlay
"""

class SafetyService:
    """Content safety validation using Azure Content Moderator"""
    
    def __init__(self):
        self.client = ContentSafetyClient(
            settings.AZURE_CONTENT_MODERATOR_ENDPOINT,
            AzureKeyCredential(settings.AZURE_CONTENT_MODERATOR_KEY)
        )
    
    def check_keywords(self, text: str) -> Tuple[bool, str]:
        """Quick keyword-based filter (free, instant)"""
        text_lower = text.lower()
        for keyword in BLOCKED_KEYWORDS:
            if keyword in text_lower:
                return False, f"Blocked keyword detected: {keyword}"
        return True, ""
    
    async def validate_text(self, text: str) -> Tuple[bool, str]:
        """Validate text using Azure Content Moderator"""
        # First: Quick keyword check
        is_safe, reason = self.check_keywords(text)
        if not is_safe:
            return False, reason
        
        # Second: Azure Content Moderator
        try:
            request = AnalyzeTextOptions(text=text)
            response = self.client.analyze_text(request)
            
            # Check if any category is flagged
            if response.hate_result and response.hate_result.severity >= 2:
                return False, "Content contains hate speech"
            if response.self_harm_result and response.self_harm_result.severity >= 2:
                return False, "Content contains self-harm references"
            if response.sexual_result and response.sexual_result.severity >= 2:
                return False, "Content contains sexual references"
            if response.violence_result and response.violence_result.severity >= 2:
                return False, "Content contains violence"
            
            return True, ""
        except Exception as e:
            # If Azure fails, fall back to keyword check only
            return True, ""  # Allow if Azure is down
    
    async def validate_image(self, image_bytes: bytes) -> Tuple[bool, str]:
        """Validate generated image using Azure Content Moderator"""
        try:
            # Convert to base64
            image_b64 = base64.b64encode(image_bytes).decode('utf-8')
            
            request = AnalyzeImageOptions(image=ImageData(content=image_b64))
            response = self.client.analyze_image(request)
            
            # Check if flagged
            if response.hate_result and response.hate_result.severity >= 2:
                return False, "Image contains inappropriate content"
            if response.sexual_result and response.sexual_result.severity >= 2:
                return False, "Image contains inappropriate content"
            if response.violence_result and response.violence_result.severity >= 2:
                return False, "Image contains violence"
            
            return True, ""
        except Exception as e:
            # If Azure fails, allow (don't block legitimate content)
            return True, ""
    
    def get_safety_negative_prompt(self) -> str:
        """Get the standard safety negative prompt"""
        return SAFETY_NEGATIVE_PROMPT.strip()
    
    def build_safe_prompt(self, user_prompt: str) -> str:
        """Add safety modifiers to user prompt"""
        safety_additions = """
        fully clothed, decent attire, appropriate clothing, 
        family-friendly, culturally respectful, elegant, 
        professional photography, high-quality portrait,
        SFW, safe for work, appropriate content
        """
        return f"{user_prompt}, {safety_additions.strip()}"