# services/svc-face/app/app/services/fal_client.py
from __future__ import annotations
from typing import Dict, Any, Optional
import fal_client
from app.config import settings

class FalClient:
    """Client for fal.ai Flux image generation"""
    
    def __init__(self):
        self.api_key = settings.FAL_API_KEY
        self.model = settings.FAL_MODEL
        fal_client.api_key = self.api_key
    
    async def generate_image(
        self,
        prompt: str,
        negative_prompt: str,
        seed: int,
        width: int = 1024,
        height: int = 1024,
        num_inference_steps: int = 28,
        guidance_scale: float = 3.5
    ) -> Dict[str, Any]:
        """
        Generate image using Flux Pro (text-to-image).
        
        Returns:
            {
                "url": "https://...",
                "width": 1024,
                "height": 1024,
                "content_type": "image/jpeg"
            }
        """
        try:
            result = await fal_client.run_async(
                self.model,
                arguments={
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "image_size": {
                        "width": width,
                        "height": height
                    },
                    "num_inference_steps": num_inference_steps,
                    "guidance_scale": guidance_scale,
                    "num_images": 1,
                    "seed": seed,
                    "enable_safety_checker": False,  # We use Azure Content Moderator
                    "sync_mode": True
                }
            )
            
            if not result or "images" not in result or not result["images"]:
                raise Exception("No image returned from fal.ai")
            
            image_data = result["images"][0]
            
            return {
                "url": image_data["url"],
                "width": image_data.get("width", width),
                "height": image_data.get("height", height),
                "content_type": image_data.get("content_type", "image/jpeg")
            }
        
        except Exception as e:
            raise Exception(f"fal.ai generation failed: {str(e)}")
    
    async def generate_image_to_image(
        self,
        prompt: str,
        negative_prompt: str,
        image_url: str,
        strength: float = 0.3,
        seed: int = 0,
        width: int = 1024,
        height: int = 1024,
        guidance_scale: float = 3.5
    ) -> Dict[str, Any]:
        """
        Generate image using image-to-image (face preservation).
        
        Low strength (0.2-0.4) preserves facial identity.
        
        Returns same format as generate_image.
        """
        try:
            result = await fal_client.run_async(
                self.model,
                arguments={
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "image_url": image_url,
                    "image_size": {
                        "width": width,
                        "height": height
                    },
                    "strength": strength,  # CRITICAL: Low = preserve face
                    "guidance_scale": guidance_scale,
                    "num_images": 1,
                    "seed": seed,
                    "enable_safety_checker": False,
                    "sync_mode": True
                }
            )
            
            if not result or "images" not in result or not result["images"]:
                raise Exception("No image returned from fal.ai")
            
            image_data = result["images"][0]
            
            return {
                "url": image_data["url"],
                "width": image_data.get("width", width),
                "height": image_data.get("height", height),
                "content_type": image_data.get("content_type", "image/jpeg")
            }
        
        except Exception as e:
            raise Exception(f"fal.ai image-to-image failed: {str(e)}")