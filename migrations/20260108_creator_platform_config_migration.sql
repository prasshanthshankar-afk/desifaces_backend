BEGIN;

-- ============================================================================
-- 1. IMAGE FORMATS TABLE - Platform-specific dimensions and specs
-- ============================================================================
CREATE TABLE face_generation_image_formats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT UNIQUE NOT NULL,
    display_name JSONB NOT NULL,  -- {"en": "Instagram Portrait", "hi": "इंस्टाग्राम पोर्ट्रेट"}
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    aspect_ratio TEXT NOT NULL,
    platform_category TEXT NOT NULL, -- 'social_media', 'professional', 'advertising', 'print'
    recommended_platforms TEXT[], -- ['instagram', 'facebook', 'tiktok']
    technical_specs JSONB DEFAULT '{}', -- DPI, color profile, etc.
    safe_zones JSONB DEFAULT '{}', -- Areas to keep clear for text/UI overlays
    is_active BOOLEAN DEFAULT true,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- 2. USE CASES TABLE - Content creation purposes and contexts
-- ============================================================================
CREATE TABLE face_generation_use_cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT UNIQUE NOT NULL,
    display_name JSONB NOT NULL,
    category TEXT NOT NULL, -- 'social_media', 'professional', 'commercial', 'artistic'
    description JSONB,
    prompt_base TEXT NOT NULL, -- Base prompt modifications for this use case
    lighting_style TEXT, -- 'natural', 'studio', 'dramatic', 'soft'
    composition_style TEXT, -- 'headshot', 'three_quarter', 'full_body'
    mood_descriptors TEXT, -- 'confident', 'approachable', 'authoritative'
    background_type TEXT, -- 'studio', 'office', 'outdoor', 'lifestyle'
    recommended_formats TEXT[], -- Codes from image_formats table
    target_audience TEXT, -- 'b2b', 'b2c', 'general', 'professional'
    industry_focus TEXT[], -- ['tech', 'finance', 'healthcare', 'entertainment']
    is_active BOOLEAN DEFAULT true,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- 3. PLATFORM REQUIREMENTS TABLE - Platform-specific constraints
-- ============================================================================
CREATE TABLE platform_requirements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_code TEXT UNIQUE NOT NULL,
    display_name JSONB NOT NULL,
    brand_colors JSONB DEFAULT '{}', -- Platform brand color schemes
    content_guidelines JSONB DEFAULT '{}', -- Platform-specific content rules
    technical_constraints JSONB DEFAULT '{}', -- File size, format restrictions
    safe_zones JSONB DEFAULT '{}', -- UI element placement areas
    recommended_formats TEXT[], -- Image format codes that work best
    api_requirements JSONB DEFAULT '{}', -- If we integrate with platform APIs
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- 4. CREATIVE VARIATIONS TABLE - Style variations within use cases
-- ============================================================================
CREATE TABLE face_generation_variations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    variation_type TEXT NOT NULL, -- 'lighting', 'pose', 'expression', 'styling'
    code TEXT NOT NULL,
    display_name JSONB NOT NULL,
    prompt_modifier TEXT NOT NULL,
    use_case_compatibility TEXT[], -- Which use cases this works with
    mood_impact TEXT, -- How this affects the overall mood
    professional_level INTEGER DEFAULT 5, -- 1-10 professionalism scale
    creativity_level INTEGER DEFAULT 5, -- 1-10 creativity scale
    is_active BOOLEAN DEFAULT true,
    UNIQUE(variation_type, code)
);

-- ============================================================================
-- 5. AGE RANGES TABLE - Standardized age groupings
-- ============================================================================
CREATE TABLE face_generation_age_ranges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT UNIQUE NOT NULL,
    display_name JSONB NOT NULL,
    min_age INTEGER NOT NULL,
    max_age INTEGER NOT NULL,
    prompt_descriptor TEXT NOT NULL,
    professional_contexts TEXT[], -- Where this age range is most appropriate
    is_active BOOLEAN DEFAULT true
);

-- ============================================================================
-- SEED DATA: IMAGE FORMATS
-- ============================================================================
INSERT INTO face_generation_image_formats (code, display_name, width, height, aspect_ratio, platform_category, recommended_platforms, technical_specs, safe_zones) VALUES

-- Social Media Formats
('instagram_portrait', '{"en": "Instagram Portrait", "hi": "इंस्टाग्राम पोर्ट्रेट"}', 1080, 1350, '4:5', 'social_media', ARRAY['instagram', 'facebook'], 
 '{"dpi": 72, "color_profile": "sRGB", "max_file_size": "10MB"}',
 '{"top": 120, "bottom": 120, "left": 80, "right": 80}'),

('instagram_story', '{"en": "Instagram Story", "hi": "इंस्टाग्राम स्टोरी"}', 1080, 1920, '9:16', 'social_media', ARRAY['instagram', 'snapchat', 'tiktok'],
 '{"dpi": 72, "color_profile": "sRGB", "max_file_size": "10MB"}',
 '{"top": 200, "bottom": 200, "left": 100, "right": 100}'),

('youtube_thumbnail', '{"en": "YouTube Thumbnail", "hi": "यूट्यूब थंबनेल"}', 1280, 720, '16:9', 'social_media', ARRAY['youtube'],
 '{"dpi": 72, "color_profile": "sRGB", "min_file_size": "2MB"}',
 '{"top": 50, "bottom": 50, "left": 100, "right": 100}'),

('tiktok_vertical', '{"en": "TikTok Vertical", "hi": "टिकटॉक वर्टिकल"}', 1080, 1920, '9:16', 'social_media', ARRAY['tiktok', 'instagram_reels'],
 '{"dpi": 72, "color_profile": "sRGB", "max_file_size": "15MB"}',
 '{"top": 150, "bottom": 150, "left": 80, "right": 80}'),

('linkedin_profile', '{"en": "LinkedIn Profile", "hi": "लिंकडइन प्रोफाइल"}', 400, 400, '1:1', 'professional', ARRAY['linkedin'],
 '{"dpi": 96, "color_profile": "sRGB", "max_file_size": "8MB"}',
 '{"top": 20, "bottom": 20, "left": 20, "right": 20}'),

('linkedin_banner', '{"en": "LinkedIn Banner", "hi": "लिंकडइन बैनर"}', 1584, 396, '4:1', 'professional', ARRAY['linkedin'],
 '{"dpi": 72, "color_profile": "sRGB", "max_file_size": "8MB"}',
 '{"top": 40, "bottom": 40, "left": 200, "right": 200}'),

-- Professional Formats
('corporate_headshot', '{"en": "Corporate Headshot", "hi": "कॉर्पोरेट हेडशॉट"}', 2048, 2048, '1:1', 'professional', ARRAY['website', 'business_cards'],
 '{"dpi": 300, "color_profile": "Adobe RGB", "format": "PNG"}',
 '{"top": 100, "bottom": 100, "left": 100, "right": 100}'),

('business_card', '{"en": "Business Card Photo", "hi": "बिजनेस कार्ड फोटो"}', 1200, 1200, '1:1', 'professional', ARRAY['print'],
 '{"dpi": 300, "color_profile": "CMYK", "format": "TIFF"}',
 '{"top": 50, "bottom": 50, "left": 50, "right": 50}'),

-- Advertising Formats
('ad_square', '{"en": "Square Ad", "hi": "स्क्वायर विज्ञापन"}', 1200, 1200, '1:1', 'advertising', ARRAY['facebook', 'instagram'],
 '{"dpi": 72, "color_profile": "sRGB", "max_file_size": "5MB"}',
 '{"top": 100, "bottom": 100, "left": 100, "right": 100}'),

('ad_landscape', '{"en": "Landscape Ad", "hi": "लैंडस्केप विज्ञापन"}', 1200, 628, '1.91:1', 'advertising', ARRAY['facebook', 'google_ads'],
 '{"dpi": 72, "color_profile": "sRGB", "max_file_size": "5MB"}',
 '{"top": 50, "bottom": 50, "left": 100, "right": 100}'),

-- Print Formats  
('magazine_portrait', '{"en": "Magazine Portrait", "hi": "पत्रिका पोर्ट्रेट"}', 3000, 4000, '3:4', 'print', ARRAY['magazine', 'editorial'],
 '{"dpi": 300, "color_profile": "CMYK", "format": "TIFF"}',
 '{"top": 200, "bottom": 200, "left": 150, "right": 150}');

-- ============================================================================
-- SEED DATA: USE CASES  
-- ============================================================================
INSERT INTO face_generation_use_cases (code, display_name, category, description, prompt_base, lighting_style, composition_style, mood_descriptors, background_type, recommended_formats, target_audience, industry_focus) VALUES

-- Social Media Use Cases
('influencer_content', '{"en": "Influencer Content", "hi": "इन्फ्लुएंसर कंटेंट"}', 'social_media',
 '{"en": "Engaging content for social media influence"}',
 'social media influencer style, engaging pose, trendy aesthetic, Instagram-ready, authentic personality',
 'natural', 'three_quarter', 'confident, approachable, trendy', 'lifestyle',
 ARRAY['instagram_portrait', 'instagram_story'], 'b2c', ARRAY['fashion', 'lifestyle', 'beauty']),

('brand_ambassador', '{"en": "Brand Ambassador", "hi": "ब्रांड एंबेसडर"}', 'commercial',
 '{"en": "Professional brand representation content"}',
 'brand ambassador style, product endorsement ready, commercial appeal, trustworthy presence',
 'studio', 'headshot', 'trustworthy, professional, aspirational', 'clean_studio',
 ARRAY['ad_square', 'linkedin_profile'], 'b2b', ARRAY['tech', 'finance', 'healthcare']),

('content_creator', '{"en": "Content Creator", "hi": "कंटेंट क्रिएटर"}', 'social_media',
 '{"en": "YouTube and content creation ready"}',
 'content creator style, YouTube thumbnail ready, engaging personality, creative energy',
 'dramatic', 'three_quarter', 'energetic, creative, engaging', 'creative_studio',
 ARRAY['youtube_thumbnail', 'tiktok_vertical'], 'b2c', ARRAY['entertainment', 'education', 'tech']),

-- Professional Use Cases  
('executive_portrait', '{"en": "Executive Portrait", "hi": "कार्यकारी पोर्ट्रेट"}', 'professional',
 '{"en": "C-suite and senior executive representation"}',
 'executive presence, leadership authority, boardroom ready, sophisticated professional',
 'studio', 'headshot', 'authoritative, confident, sophisticated', 'corporate_office',
 ARRAY['corporate_headshot', 'linkedin_banner'], 'b2b', ARRAY['finance', 'consulting', 'law']),

('startup_founder', '{"en": "Startup Founder", "hi": "स्टार्टअप संस्थापक"}', 'professional',
 '{"en": "Entrepreneur and innovation leader style"}',
 'startup founder energy, innovation leader, tech entrepreneur, modern professional',
 'natural', 'three_quarter', 'visionary, approachable, innovative', 'modern_office',
 ARRAY['linkedin_profile', 'corporate_headshot'], 'b2b', ARRAY['tech', 'startups', 'innovation']),

('team_member', '{"en": "Team Member", "hi": "टीम सदस्य"}', 'professional',
 '{"en": "Company team page and about us content"}',
 'team member style, company culture fit, collaborative spirit, professional approachable',
 'soft', 'headshot', 'friendly, professional, collaborative', 'office_casual',
 ARRAY['corporate_headshot', 'linkedin_profile'], 'b2b', ARRAY['general', 'services', 'consulting']),

-- Commercial Use Cases
('advertisement_model', '{"en": "Advertisement Model", "hi": "विज्ञापन मॉडल"}', 'commercial',
 '{"en": "Commercial advertising and marketing campaigns"}',
 'commercial advertising style, product marketing ready, brand alignment, consumer appeal',
 'studio', 'full_body', 'aspirational, attractive, relatable', 'branded_backdrop',
 ARRAY['ad_landscape', 'ad_square'], 'b2c', ARRAY['retail', 'consumer_goods', 'automotive']),

('testimonial_speaker', '{"en": "Testimonial Speaker", "hi": "प्रशंसापत्र वक्ता"}', 'commercial',
 '{"en": "Customer testimonial and case study content"}',
 'satisfied customer testimonial, genuine endorsement, trustworthy recommendation, authentic satisfaction',
 'natural', 'headshot', 'satisfied, genuine, trustworthy', 'real_environment',
 ARRAY['linkedin_profile', 'ad_square'], 'b2b', ARRAY['services', 'saas', 'consulting']);

-- ============================================================================ 
-- SEED DATA: CREATIVE VARIATIONS
-- ============================================================================
INSERT INTO face_generation_variations (variation_type, code, display_name, prompt_modifier, use_case_compatibility, mood_impact, professional_level, creativity_level) VALUES

-- Lighting Variations
('lighting', 'natural_soft', '{"en": "Natural Soft", "hi": "प्राकृतिक कोमल"}',
 'soft natural lighting, window light, gentle shadows', 
 ARRAY['influencer_content', 'startup_founder'], 'warm, approachable', 7, 6),

('lighting', 'studio_dramatic', '{"en": "Studio Dramatic", "hi": "स्टूडियो नाटकीय"}',
 'dramatic studio lighting, professional photography, strong contrast',
 ARRAY['executive_portrait', 'advertisement_model'], 'powerful, authoritative', 9, 8),

('lighting', 'golden_hour', '{"en": "Golden Hour", "hi": "सुनहरा समय"}',
 'golden hour lighting, warm sunset glow, cinematic quality',
 ARRAY['influencer_content', 'brand_ambassador'], 'aspirational, dreamy', 6, 9),

-- Expression Variations
('expression', 'confident_smile', '{"en": "Confident Smile", "hi": "आत्मविश्वास से भरी मुस्कान"}',
 'confident genuine smile, approachable expression, professional warmth',
 ARRAY['team_member', 'brand_ambassador'], 'positive, trustworthy', 8, 5),

('expression', 'serious_professional', '{"en": "Serious Professional", "hi": "गंभीर पेशेवर"}',
 'serious professional expression, focused gaze, executive presence',
 ARRAY['executive_portrait', 'corporate_headshot'], 'authoritative, focused', 10, 3),

('expression', 'creative_thoughtful', '{"en": "Creative Thoughtful", "hi": "रचनात्मक विचारशील"}',
 'thoughtful creative expression, innovative spirit, visionary gaze',
 ARRAY['startup_founder', 'content_creator'], 'innovative, inspiring', 7, 9),

-- Pose Variations
('pose', 'power_stance', '{"en": "Power Stance", "hi": "शक्ति मुद्रा"}',
 'confident power pose, leadership stance, authoritative positioning',
 ARRAY['executive_portrait', 'advertisement_model'], 'commanding, confident', 9, 6),

('pose', 'approachable_casual', '{"en": "Approachable Casual", "hi": "सुलभ आकस्मिक"}',
 'relaxed approachable pose, casual professional stance, friendly positioning',
 ARRAY['team_member', 'influencer_content'], 'friendly, accessible', 6, 7),

-- Styling Variations
('styling', 'executive_formal', '{"en": "Executive Formal", "hi": "कार्यकारी औपचारिक"}',
 'executive formal styling, premium business attire, sophisticated grooming',
 ARRAY['executive_portrait', 'corporate_headshot'], 'sophisticated, authoritative', 10, 4),

('styling', 'modern_professional', '{"en": "Modern Professional", "hi": "आधुनिक पेशेवर"}',
 'modern professional styling, contemporary business casual, tech-forward look',
 ARRAY['startup_founder', 'team_member'], 'innovative, contemporary', 8, 7);

-- ============================================================================
-- SEED DATA: AGE RANGES
-- ============================================================================
INSERT INTO face_generation_age_ranges (code, display_name, min_age, max_age, prompt_descriptor, professional_contexts) VALUES

('young_professional', '{"en": "Young Professional", "hi": "युवा पेशेवर"}', 22, 28, 
 'young professional, 22-28 years old, early career energy', 
 ARRAY['startup_founder', 'team_member', 'content_creator']),

('established_professional', '{"en": "Established Professional", "hi": "स्थापित पेशेवर"}', 29, 35,
 'established professional, 29-35 years old, career growth phase',
 ARRAY['brand_ambassador', 'team_member', 'startup_founder']),

('senior_professional', '{"en": "Senior Professional", "hi": "वरिष्ठ पेशेवर"}', 36, 45,
 'senior professional, 36-45 years old, leadership experience',
 ARRAY['executive_portrait', 'testimonial_speaker', 'brand_ambassador']),

('executive_level', '{"en": "Executive Level", "hi": "कार्यकारी स्तर"}', 46, 60,
 'executive level, 46-60 years old, senior leadership presence',
 ARRAY['executive_portrait', 'testimonial_speaker']);

-- ============================================================================
-- SEED DATA: PLATFORM REQUIREMENTS
-- ============================================================================
INSERT INTO platform_requirements (platform_code, display_name, brand_colors, content_guidelines, technical_constraints, safe_zones, recommended_formats) VALUES

('instagram', '{"en": "Instagram", "hi": "इंस्टाग्राम"}',
 '{"primary": "#E4405F", "secondary": "#FFDC00", "text": "#262626"}',
 '{"max_text_overlay": "20%", "brand_prominence": "minimal", "authenticity": "high"}',
 '{"max_file_size": "10MB", "formats": ["JPG", "PNG"], "compression": "high"}',
 '{"story_top": 200, "story_bottom": 200, "feed_safe_zone": 80}',
 ARRAY['instagram_portrait', 'instagram_story']),

('linkedin', '{"en": "LinkedIn", "hi": "लिंकडइन"}',
 '{"primary": "#0077B5", "secondary": "#00A0DC", "text": "#2E2E2E"}',
 '{"professionalism": "high", "personal_branding": "encouraged", "industry_focus": "preferred"}',
 '{"max_file_size": "8MB", "formats": ["JPG", "PNG"], "min_resolution": "400x400"}',
 '{"profile_safe_zone": 20, "banner_text_area": 200}',
 ARRAY['linkedin_profile', 'linkedin_banner']),

('youtube', '{"en": "YouTube", "hi": "यूट्यूब"}',
 '{"primary": "#FF0000", "secondary": "#282828", "text": "#FFFFFF"}',
 '{"thumbnail_text": "encouraged", "emotion_expression": "high", "click_appeal": "maximum"}',
 '{"min_file_size": "2MB", "formats": ["JPG", "PNG"], "aspect_ratio": "16:9"}',
 '{"text_safe_zone": 100, "mobile_safe_zone": 150}',
 ARRAY['youtube_thumbnail']);

COMMIT;