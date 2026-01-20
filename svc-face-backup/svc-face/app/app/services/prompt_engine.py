# services/svc-face/app/services/enhanced_prompt_engine.py
from __future__ import annotations
import random
import logging
from typing import Dict, List, Any, Tuple
import asyncpg
from app.repos.config_repo import FaceConfigRepo

logger = logging.getLogger("enhanced_prompt_engine")

class DatabaseDrivenDiversityEngine:
    """
    Advanced diversity engine that uses the face_generation config tables
    to create genuinely diverse prompts by systematically varying:
    - Regional features
    - Skin tones  
    - Facial features (jaw, nose, eyes, lips, cheekbones)
    - Socioeconomic contexts
    - Age groups
    - Body structures
    
    Solves the "monotonous faces" problem by forcing systematic variation.
    """
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.config_repo = FaceConfigRepo(pool)
        
    async def generate_diverse_prompts(
        self,
        user_request: Dict[str, Any],
        region_config: Dict[str, Any],
        num_variants: int = 4
    ) -> List[Dict[str, str]]:
        """
        Generate truly diverse prompts using database-driven systematic variation
        """
        
        # 1. Get diversity data from database
        skin_tones = await self.config_repo.get_skin_tones()
        facial_features = await self.config_repo.get_facial_features()
        contexts = await self.config_repo.get_contexts()
        clothing_styles = await self.config_repo.get_clothing_styles()
        
        # 2. Create diversity matrix for this region
        diversity_matrix = self._create_diversity_matrix(
            region_config, skin_tones, facial_features, contexts
        )
        
        # 3. Generate variants with forced diversity
        prompts = []
        for i in range(num_variants):
            variant = self._generate_diverse_variant(
                user_request, diversity_matrix, i, num_variants
            )
            prompts.append(variant)
            
        logger.info("Generated diverse prompts", extra={
            "num_variants": len(prompts),
            "region": region_config.get("code"),
            "diversity_enforced": True
        })
        
        return prompts
    
    def _create_diversity_matrix(
        self,
        region_config: Dict[str, Any],
        skin_tones: List[Dict[str, Any]],
        facial_features: List[Dict[str, Any]],
        contexts: List[Dict[str, Any]]
    ) -> Dict[str, List[str]]:
        """Create a matrix of diversity options for this region"""
        
        # Filter skin tones appropriate for this region
        regional_skin_tones = region_config.get('typical_skin_tones', [])
        if regional_skin_tones:
            filtered_tones = [
                tone for tone in skin_tones 
                if tone['code'] in regional_skin_tones
            ]
        else:
            filtered_tones = skin_tones
            
        # Group facial features by type
        features_by_type = {}
        for feature in facial_features:
            feature_type = feature['feature_type']
            if feature_type not in features_by_type:
                features_by_type[feature_type] = []
            features_by_type[feature_type].append(feature)
        
        return {
            'skin_tones': [tone['prompt_descriptor'] for tone in filtered_tones],
            'jaw_types': [f['prompt_descriptor'] for f in features_by_type.get('jaw', [])],
            'nose_types': [f['prompt_descriptor'] for f in features_by_type.get('nose', [])],
            'eye_types': [f['prompt_descriptor'] for f in features_by_type.get('eyes', [])],
            'lip_types': [f['prompt_descriptor'] for f in features_by_type.get('lips', [])],
            'cheekbone_types': [f['prompt_descriptor'] for f in features_by_type.get('cheekbones', [])],
            'hair_types': [f['prompt_descriptor'] for f in features_by_type.get('hair', [])],
            'body_types': [f['prompt_descriptor'] for f in features_by_type.get('body', [])],
            'age_groups': ['22-26 years old', '27-31 years old', '32-37 years old', '38-44 years old', '45-52 years old'],
            'contexts': [ctx['prompt_modifiers'] for ctx in contexts if ctx['economic_class'] in ['middle', 'affluent']]
        }
    
    def _generate_diverse_variant(
        self,
        user_request: Dict[str, Any],
        diversity_matrix: Dict[str, List[str]],
        variant_index: int,
        total_variants: int
    ) -> Dict[str, str]:
        """Generate a single variant with forced diversity"""
        
        # Use systematic sampling to ensure maximum diversity
        def get_diverse_choice(options: List[str], index: int) -> str:
            if not options:
                return ""
            # Ensure different variants get different choices
            choice_index = (index * 7) % len(options)  # 7 is a good prime for distribution
            return options[choice_index]
        
        # Pick systematically different features for this variant
        skin_tone = get_diverse_choice(diversity_matrix['skin_tones'], variant_index)
        jaw_type = get_diverse_choice(diversity_matrix['jaw_types'], variant_index)
        nose_type = get_diverse_choice(diversity_matrix['nose_types'], variant_index + 1)
        eye_type = get_diverse_choice(diversity_matrix['eye_types'], variant_index + 2)
        lip_type = get_diverse_choice(diversity_matrix['lip_types'], variant_index + 3)
        cheekbone_type = get_diverse_choice(diversity_matrix['cheekbone_types'], variant_index + 4)
        hair_type = get_diverse_choice(diversity_matrix['hair_types'], variant_index + 2)
        body_type = get_diverse_choice(diversity_matrix['body_types'], variant_index + 1)
        age_group = get_diverse_choice(diversity_matrix['age_groups'], variant_index)
        context = get_diverse_choice(diversity_matrix['contexts'], variant_index)
        
        # Build the diverse prompt
        base_prompt = f"""
{age_group}, {user_request['gender']} from {user_request.get('region', 'India')},
{skin_tone}, {jaw_type}, {nose_type}, {eye_type}, {lip_type}, {cheekbone_type},
{hair_type}, {body_type}, {user_request.get('style', 'professional')} style,
{context}, high-quality portrait photography, natural lighting, authentic Indian beauty,
diversity emphasis, unique facial structure, distinctly different appearance
""".strip().replace('\n', ' ').replace('  ', ' ')
        
        # Create negative prompt to avoid generic looks
        negative_prompt = f"""
generic face, monotonous appearance, repetitive features, clone-like similarity,
western features, overly fair skin, overly perfect features, artificial beauty,
heavily retouched, plastic surgery look, identical to other variants,
boring, typical, standard, common, ordinary, uninteresting, bland
"""
        
        return {
            "prompt": base_prompt,
            "negative_prompt": negative_prompt,
            "diversity_traits": {
                "skin_tone": skin_tone,
                "age_group": age_group,
                "facial_features": f"{jaw_type}, {nose_type}, {eye_type}",
                "variant_index": variant_index
            }
        }
    
    async def validate_diversity(self, prompts: List[Dict[str, str]]) -> bool:
        """Validate that the generated prompts are actually diverse"""
        
        # Extract key diversity traits from each prompt
        traits_sets = []
        for prompt_data in prompts:
            traits = prompt_data.get('diversity_traits', {})
            trait_signature = f"{traits.get('skin_tone', '')}-{traits.get('age_group', '')}-{traits.get('facial_features', '')}"
            traits_sets.append(trait_signature)
        
        # Check uniqueness
        unique_traits = len(set(traits_sets))
        diversity_ratio = unique_traits / len(prompts)
        
        logger.info("Diversity validation", extra={
            "total_variants": len(prompts),
            "unique_combinations": unique_traits,
            "diversity_ratio": diversity_ratio,
            "passed": diversity_ratio >= 0.8
        })
        
        return diversity_ratio >= 0.8  # At least 80% should be unique
    
    async def enhance_prompt_with_safety(
        self, 
        prompt_data: Dict[str, str], 
        safety_negative: str
    ) -> Dict[str, str]:
        """Enhance prompt with safety while preserving diversity"""
        
        enhanced = prompt_data.copy()
        
        # Add safety to negative prompt
        enhanced["negative_prompt"] = f"{prompt_data['negative_prompt']}, {safety_negative}"
        
        # Add diversity emphasis to main prompt
        enhanced["prompt"] = f"{prompt_data['prompt']}, photorealistic, authentic representation, cultural accuracy"
        
        return enhanced