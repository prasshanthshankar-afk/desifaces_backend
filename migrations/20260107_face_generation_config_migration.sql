-- Migration: Face Generation Configuration System
-- Date: 2026-01-08
-- Purpose: Database-driven regional diversity for DesiFaces

BEGIN;

-- ============================================================================
-- 1. REGIONS TABLE - Complete India Representation
-- ============================================================================
CREATE TABLE face_generation_regions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT UNIQUE NOT NULL,
    display_name JSONB NOT NULL,  -- Multilingual
    sub_region TEXT,
    ethnicity_notes TEXT,
    typical_skin_tones TEXT[],
    traditional_attire JSONB,
    cultural_markers JSONB,
    prompt_base TEXT NOT NULL,
    is_active BOOLEAN DEFAULT true,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================================
-- 2. SKIN TONES TABLE - Complete Spectrum
-- ============================================================================
CREATE TABLE face_generation_skin_tones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT UNIQUE NOT NULL,
    display_name JSONB NOT NULL,
    hex_reference TEXT,
    prompt_descriptor TEXT NOT NULL,
    diversity_weight INTEGER DEFAULT 1,  -- Higher = prioritize more
    is_active BOOLEAN DEFAULT true
);

-- ============================================================================
-- 3. FACIAL FEATURES TABLE - Anthropometric Diversity
-- ============================================================================
CREATE TABLE face_generation_features (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_type TEXT NOT NULL,  -- 'jaw', 'nose', 'eyes', 'lips', 'cheekbones'
    code TEXT NOT NULL,
    prompt_descriptor TEXT NOT NULL,
    is_active BOOLEAN DEFAULT true,
    UNIQUE(feature_type, code)
);

-- ============================================================================
-- 4. SOCIOECONOMIC CONTEXTS TABLE
-- ============================================================================
CREATE TABLE face_generation_contexts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT UNIQUE NOT NULL,
    display_name JSONB NOT NULL,
    economic_class TEXT,  -- 'working', 'middle', 'affluent', 'elite'
    setting_type TEXT,  -- 'rural', 'urban', 'metro', 'global'
    attire_style JSONB,
    background_prompts TEXT[],
    prompt_modifiers TEXT NOT NULL,
    glamour_level INTEGER,  -- 1=natural, 5=bollywood, 10=editorial
    is_active BOOLEAN DEFAULT true
);

-- ============================================================================
-- 5. CLOTHING STYLES TABLE
-- ============================================================================
CREATE TABLE face_generation_clothing (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT UNIQUE NOT NULL,
    display_name JSONB NOT NULL,
    category TEXT,  -- 'traditional', 'modern', 'fusion', 'glamour'
    gender_fit TEXT,  -- 'male', 'female', 'neutral'
    regions TEXT[],  -- Which regions this applies to
    prompt_descriptor TEXT NOT NULL,
    formality_level INTEGER,  -- 1=casual, 10=formal
    is_active BOOLEAN DEFAULT true
);

-- ============================================================================
-- SEED DATA: REGIONS (Complete India)
-- ============================================================================
INSERT INTO face_generation_regions (code, display_name, sub_region, ethnicity_notes, typical_skin_tones, prompt_base) VALUES

-- North India
('punjab', '{"en": "Punjab", "hi": "पंजाब", "pa": "ਪੰਜਾਬ"}', 'North', 'Punjabi, Jat, Khatri heritage', 
 ARRAY['fair', 'wheatish', 'medium_brown'], 
 'Punjabi heritage, strong features, confident presence'),

('haryana', '{"en": "Haryana", "hi": "हरियाणा"}', 'North', 'Haryanvi, Jat features',
 ARRAY['fair', 'wheatish'],
 'Haryanvi heritage, athletic build, rural strength'),

('himachal', '{"en": "Himachal Pradesh", "hi": "हिमाचल प्रदेश"}', 'North', 'Pahari features',
 ARRAY['fair', 'very_fair'],
 'Himalayan Pahari features, mountain heritage, delicate features'),

('uttarakhand', '{"en": "Uttarakhand", "hi": "उत्तराखंड"}', 'North', 'Garhwali, Kumaoni',
 ARRAY['fair', 'wheatish'],
 'Garhwali Kumaoni heritage, mountain people, strong features'),

('delhi_ncr', '{"en": "Delhi NCR", "hi": "दिल्ली एनसीआर"}', 'North', 'Cosmopolitan mix',
 ARRAY['fair', 'wheatish', 'medium_brown'],
 'Delhi cosmopolitan modern Indian, urban sophistication'),

('uttar_pradesh', '{"en": "Uttar Pradesh", "hi": "उत्तर प्रदेश"}', 'North', 'Diverse UP heritage',
 ARRAY['fair', 'wheatish', 'medium_brown'],
 'UP heritage, diverse features, heartland India'),

('rajasthan', '{"en": "Rajasthan", "hi": "राजस्थान"}', 'North', 'Rajput, Marwari features',
 ARRAY['fair', 'wheatish'],
 'Rajasthani heritage, regal features, desert beauty'),

-- East India
('bengal', '{"en": "West Bengal", "hi": "पश्चिम बंगाल", "bn": "পশ্চিমবঙ্গ"}', 'East', 'Bengali features',
 ARRAY['fair', 'medium_brown'],
 'Bengali heritage, refined features, cultural sophistication'),

('odisha', '{"en": "Odisha", "hi": "ओडिशा"}', 'East', 'Odia features',
 ARRAY['medium_brown', 'deep_brown'],
 'Odia heritage, coastal features, temple culture'),

('jharkhand', '{"en": "Jharkhand", "hi": "झारखंड"}', 'East', 'Tribal diversity',
 ARRAY['medium_brown', 'deep_brown', 'dark'],
 'Jharkhand tribal heritage, indigenous features, forest people'),

('bihar', '{"en": "Bihar", "hi": "बिहार"}', 'East', 'Bihari, Maithili',
 ARRAY['wheatish', 'medium_brown'],
 'Bihari heritage, heartland features, rural authenticity'),

-- Northeast India
('assam', '{"en": "Assam", "hi": "असम"}', 'Northeast', 'Assamese, Ahom features',
 ARRAY['wheatish', 'medium_brown'],
 'Assamese heritage, northeast features, tea garden beauty'),

('nagaland', '{"en": "Nagaland"}', 'Northeast', 'Naga tribal features',
 ARRAY['medium_brown', 'wheatish'],
 'Naga tribal heritage, distinct northeast features, warrior tradition'),

('manipur', '{"en": "Manipur"}', 'Northeast', 'Meitei features',
 ARRAY['fair', 'wheatish'],
 'Manipuri heritage, East Asian features, graceful beauty'),

('meghalaya', '{"en": "Meghalaya"}', 'Northeast', 'Khasi, Garo features',
 ARRAY['medium_brown', 'wheatish'],
 'Meghalaya tribal heritage, matrilineal society, hill people'),

('mizoram', '{"en": "Mizoram"}', 'Northeast', 'Mizo features',
 ARRAY['wheatish', 'fair'],
 'Mizo heritage, distinct features, highland beauty'),

-- West India
('maharashtra', '{"en": "Maharashtra", "hi": "महाराष्ट्र", "mr": "महाराष्ट्र"}', 'West', 'Marathi features',
 ARRAY['wheatish', 'medium_brown'],
 'Marathi heritage, diverse features, cosmopolitan blend'),

('gujarat', '{"en": "Gujarat", "hi": "गुजरात", "gu": "ગુજરાત"}', 'West', 'Gujarati features',
 ARRAY['fair', 'wheatish'],
 'Gujarati heritage, business class elegance, western India'),

('goa', '{"en": "Goa"}', 'West', 'Goan Konkani features',
 ARRAY['medium_brown', 'wheatish'],
 'Goan heritage, coastal beauty, Portuguese influence, beach culture'),

-- South India
('tamil_nadu', '{"en": "Tamil Nadu", "hi": "तमिलनाडु", "ta": "தமிழ்நாடு"}', 'South', 'Tamil Dravidian',
 ARRAY['deep_brown', 'dark', 'medium_brown'],
 'Tamil heritage, Dravidian features, temple culture, classical beauty'),

('kerala', '{"en": "Kerala", "hi": "केरल", "ml": "കേരളം"}', 'South', 'Malayali features',
 ARRAY['medium_brown', 'deep_brown', 'dark'],
 'Malayali heritage, Kerala features, coastal elegance, communist pride'),

('karnataka', '{"en": "Karnataka", "hi": "कर्नाटक", "kn": "ಕರ್ನಾಟಕ"}', 'South', 'Kannada features',
 ARRAY['medium_brown', 'wheatish', 'deep_brown'],
 'Kannada heritage, tech city blend, diverse features'),

('andhra_pradesh', '{"en": "Andhra Pradesh", "hi": "आंध्र प्रदेश", "te": "ఆంధ్ర ప్రదేశ్"}', 'South', 'Telugu features',
 ARRAY['medium_brown', 'deep_brown'],
 'Telugu heritage, Andhra features, coastal sophistication'),

('telangana', '{"en": "Telangana", "te": "తెలంగాణ"}', 'South', 'Telangana features',
 ARRAY['medium_brown', 'deep_brown'],
 'Telangana heritage, Hyderabadi blend, Deccan features'),

-- Central India
('madhya_pradesh', '{"en": "Madhya Pradesh", "hi": "मध्य प्रदेश"}', 'Central', 'MP tribal & mainstream',
 ARRAY['wheatish', 'medium_brown'],
 'Madhya Pradesh heritage, heartland features, tribal diversity'),

('chhattisgarh', '{"en": "Chhattisgarh", "hi": "छत्तीसगढ़"}', 'Central', 'Tribal features',
 ARRAY['medium_brown', 'deep_brown'],
 'Chhattisgarh tribal heritage, indigenous features, forest culture'),

-- Modern/Cosmopolitan
('modern_india', '{"en": "Modern India", "hi": "आधुनिक भारत"}', 'Pan-India', 'Pan-Indian urban',
 ARRAY['fair', 'wheatish', 'medium_brown', 'deep_brown'],
 'Modern urban Indian, cosmopolitan blend, global style'),

('nri_global', '{"en": "NRI/Global Indian", "hi": "प्रवासी भारतीय"}', 'Global', 'Global Indian diaspora',
 ARRAY['fair', 'wheatish', 'medium_brown'],
 'NRI global Indian, international style, diaspora elegance');

-- ============================================================================
-- SEED DATA: SKIN TONES (Complete Spectrum)
-- ============================================================================
INSERT INTO face_generation_skin_tones (code, display_name, hex_reference, prompt_descriptor, diversity_weight) VALUES
('very_fair', '{"en": "Very Fair", "hi": "बहुत गोरा"}', '#FFE4C4', 'very fair porcelain skin', 2),
('fair', '{"en": "Fair", "hi": "गोरा"}', '#F5DEB3', 'fair creamy skin tone', 3),
('wheatish', '{"en": "Wheatish", "hi": "गेहुंआ"}', '#D2B48C', 'wheatish golden skin', 4),
('medium_brown', '{"en": "Medium Brown", "hi": "मध्यम भूरा"}', '#8B7355', 'medium brown warm skin', 5),
('deep_brown', '{"en": "Deep Brown", "hi": "गहरा भूरा"}', '#654321', 'deep brown rich skin', 5),
('dark', '{"en": "Dark", "hi": "काला"}', '#3B2414', 'dark chocolate ebony skin', 4);

-- ============================================================================
-- SEED DATA: FACIAL FEATURES (Anthropometric Diversity)
-- ============================================================================
INSERT INTO face_generation_features (feature_type, code, prompt_descriptor) VALUES
-- Jaw shapes
('jaw', 'sharp', 'sharp defined jawline'),
('jaw', 'rounded', 'soft rounded jaw'),
('jaw', 'square', 'strong square jaw'),
('jaw', 'delicate', 'delicate refined jawline'),

-- Cheekbones
('cheekbones', 'high', 'high prominent cheekbones'),
('cheekbones', 'soft', 'soft subtle cheekbones'),
('cheekbones', 'flat', 'flat cheekbones'),
('cheekbones', 'sculpted', 'sculpted defined cheekbones'),

-- Nose shapes
('nose', 'narrow', 'narrow refined nose'),
('nose', 'broad', 'broad nose'),
('nose', 'aquiline', 'aquiline hooked nose'),
('nose', 'button', 'small button nose'),
('nose', 'long', 'long elegant nose'),
('nose', 'flat', 'flat bridge nose'),

-- Lips
('lips', 'full', 'full plump lips'),
('lips', 'thin', 'thin delicate lips'),
('lips', 'heart', 'heart-shaped cupid bow lips'),
('lips', 'wide', 'wide expressive lips'),

-- Eyes
('eyes', 'almond', 'almond-shaped eyes'),
('eyes', 'round', 'large round eyes'),
('eyes', 'hooded', 'hooded deep-set eyes'),
('eyes', 'monolid', 'monolid eyes'),
('eyes', 'wide', 'wide expressive eyes'),

-- Hair types
('hair', 'straight_long', 'long straight hair'),
('hair', 'wavy_medium', 'shoulder-length wavy hair'),
('hair', 'curly_thick', 'thick curly hair'),
('hair', 'coily_textured', 'coily textured natural hair'),
('hair', 'short_modern', 'short modern haircut'),
('hair', 'braided', 'traditional braided hair'),
('hair', 'bun_elegant', 'elegant hair bun'),

-- Body structure
('body', 'petite', 'petite delicate frame'),
('body', 'tall', 'tall elegant stature'),
('body', 'athletic', 'athletic fit physique'),
('body', 'curvy', 'curvy voluptuous figure'),
('body', 'lean', 'lean slim build'),
('body', 'stocky', 'stocky strong build');

-- ============================================================================
-- SEED DATA: SOCIOECONOMIC CONTEXTS (Complete Spectrum)
-- ============================================================================
INSERT INTO face_generation_contexts (code, display_name, economic_class, setting_type, prompt_modifiers, glamour_level) VALUES

-- Rural/Working Class (Glamour 1-2)
('rural_farmer', '{"en": "Rural Farmer", "hi": "ग्रामीण किसान"}', 'working', 'rural',
 'rural farmer, simple cotton clothes, village background, natural lighting, authentic rural India, fields in background', 1),

('tribal_indigenous', '{"en": "Tribal Indigenous", "hi": "आदिवासी"}', 'working', 'rural',
 'tribal indigenous person, traditional tribal textiles, forest background, natural authentic look, tribal jewelry', 1),

('village_daily_worker', '{"en": "Village Worker", "hi": "गांव का मजदूर"}', 'working', 'rural',
 'village daily wage worker, simple working clothes, rural market background, authentic representation', 1),

('fisher_folk', '{"en": "Fishing Community", "hi": "मछुआरा समुदाय"}', 'working', 'rural',
 'fishing community member, coastal village attire, beach background, nets and boats, authentic coastal life', 1),

-- Middle Class (Glamour 3-5)
('student_middle_class', '{"en": "Student", "hi": "छात्र"}', 'middle', 'urban',
 'middle-class student, casual modern clothes, college campus, books, youthful energy', 3),

('office_professional', '{"en": "Office Professional", "hi": "कार्यालय पेशेवर"}', 'middle', 'urban',
 'corporate office professional, business casual attire, modern office background, confident pose', 4),

('small_business', '{"en": "Small Business Owner", "hi": "छोटा व्यवसायी"}', 'middle', 'urban',
 'small business owner, smart casual wear, shop background, entrepreneurial spirit', 4),

-- Affluent (Glamour 6-7)
('upper_middle_affluent', '{"en": "Affluent Professional", "hi": "संपन्न पेशेवर"}', 'affluent', 'metro',
 'affluent professional, premium designer wear, modern metro backdrop, sophisticated style', 6),

('tech_executive', '{"en": "Tech Executive", "hi": "तकनीकी अधिकारी"}', 'affluent', 'metro',
 'tech industry executive, smart luxury casual, Bangalore/Mumbai skyline, modern Indian success', 7),

-- Elite/Bollywood (Glamour 8-10)
('bollywood_glam', '{"en": "Bollywood Star", "hi": "बॉलीवुड स्टार"}', 'elite', 'metro',
 'Bollywood celebrity style, high-fashion glamorous outfit, cinematic lighting, star presence, sensual elegance, red carpet worthy', 9),

('fashion_editorial', '{"en": "Fashion Editorial", "hi": "फैशन एडिटोरियल"}', 'elite', 'global',
 'high-fashion editorial model, couture designer wear, studio lighting, Vogue-worthy, international runway style', 10),

('elite_socialite', '{"en": "Elite Socialite", "hi": "अभिजात समाजी"}', 'elite', 'metro',
 'elite Indian socialite, luxury designer wear, five-star hotel background, diamond jewelry, sophisticated glamour', 9),

('global_influencer', '{"en": "Global Influencer", "hi": "वैश्विक प्रभावशाली"}', 'elite', 'global',
 'global Indian influencer, ultra-modern fashion, international location, Instagram-worthy, trendsetter style', 8);

-- ============================================================================
-- SEED DATA: CLOTHING STYLES (Complete Diversity)
-- ============================================================================
INSERT INTO face_generation_clothing (code, display_name, category, gender_fit, prompt_descriptor, formality_level) VALUES

-- Traditional Female
('saree_traditional', '{"en": "Traditional Saree", "hi": "पारंपरिक साड़ी"}', 'traditional', 'female',
 'traditional silk saree with blouse, elegant draping, cultural jewelry', 7),

('lehenga_festive', '{"en": "Festive Lehenga", "hi": "त्योहार लहंगा"}', 'traditional', 'female',
 'colorful festive lehenga choli, heavy embroidery, traditional jewelry, celebration wear', 8),

('salwar_kameez', '{"en": "Salwar Kameez", "hi": "सलवार कमीज़"}', 'traditional', 'female',
 'elegant salwar kameez suit, dupatta, comfortable traditional wear', 5),

('kerala_kasavu', '{"en": "Kerala Kasavu Saree", "ml": "കേരള കാസവ്"}', 'traditional', 'female',
 'Kerala traditional kasavu saree with gold border, minimal jewelry, elegant simplicity', 7),

-- Traditional Male
('kurta_pyjama', '{"en": "Kurta Pyjama", "hi": "कुर्ता पाजामा"}', 'traditional', 'male',
 'traditional kurta pyjama, nehru jacket optional, ethnic elegance', 6),

('dhoti_kurta', '{"en": "Dhoti Kurta", "hi": "धोती कुर्ता"}', 'traditional', 'male',
 'white dhoti with kurta, traditional Indian attire, cultural authenticity', 7),

('sherwani_royal', '{"en": "Sherwani", "hi": "शेरवानी"}', 'traditional', 'male',
 'royal sherwani with embroidery, turban, regal appearance, wedding attire', 9),

-- Modern/Contemporary
('jeans_tshirt', '{"en": "Casual Jeans", "hi": "जींस टी-शर्ट"}', 'modern', 'neutral',
 'casual jeans with t-shirt, modern urban style, comfortable everyday wear', 2),

('business_formal', '{"en": "Business Formal", "hi": "व्यावसायिक फॉर्मल"}', 'modern', 'neutral',
 'professional business suit, corporate attire, modern office wear', 8),

('indo_western_fusion', '{"en": "Indo-Western Fusion", "hi": "इंडो-वेस्टर्न फ्यूजन"}', 'fusion', 'neutral',
 'Indo-western fusion outfit, contemporary meets traditional, trendy style', 6),

-- Glamour/High Fashion
('designer_gown', '{"en": "Designer Gown", "hi": "डिजाइनर गाउन"}', 'glamour', 'female',
 'designer evening gown, red carpet style, high fashion, Bollywood glamour', 10),

('haute_couture', '{"en": "Haute Couture", "hi": "हाउते कउतुरे"}', 'glamour', 'female',
 'haute couture designer piece, runway fashion, editorial style, avant-garde', 10),

('elegant_cocktail', '{"en": "Cocktail Dress", "hi": "कॉकटेल ड्रेस"}', 'glamour', 'female',
 'elegant cocktail dress, party wear, sophisticated style, evening glamour', 8),

-- Tribal/Regional Specific
('tribal_textile', '{"en": "Tribal Textiles", "hi": "आदिवासी वस्त्र"}', 'traditional', 'neutral',
 'authentic tribal traditional textiles, indigenous patterns, cultural jewelry, ethnic pride', 6),

('punjabi_suit', '{"en": "Punjabi Suit", "pa": "ਪੰਜਾਬੀ ਸੂਟ"}', 'traditional', 'female',
 'colorful Punjabi suit with phulkari dupatta, vibrant patterns, traditional Punjabi style', 6),

('mundu_kerala', '{"en": "Kerala Mundu", "ml": "മുണ്ട്"}', 'traditional', 'male',
 'Kerala traditional mundu with shirt, simple elegant South Indian attire', 5);

-- ============================================================================
-- INDEXES
-- ============================================================================
CREATE INDEX idx_regions_active ON face_generation_regions(is_active);
CREATE INDEX idx_skin_tones_active ON face_generation_skin_tones(is_active);
CREATE INDEX idx_features_type ON face_generation_features(feature_type, is_active);
CREATE INDEX idx_contexts_glamour ON face_generation_contexts(glamour_level);
CREATE INDEX idx_clothing_category ON face_generation_clothing(category, is_active);

COMMIT;

-- ============================================================================
-- USAGE EXAMPLE
-- ============================================================================
/*
-- Get all active regions
SELECT code, display_name->>'en' as name, prompt_base 
FROM face_generation_regions 
WHERE is_active = true;

-- Get diverse skin tones (prioritized by weight)
SELECT code, prompt_descriptor 
FROM face_generation_skin_tones 
WHERE is_active = true 
ORDER BY diversity_weight DESC;

-- Get random facial features for diversity
SELECT feature_type, prompt_descriptor 
FROM face_generation_features 
WHERE is_active = true 
ORDER BY RANDOM() 
LIMIT 5;

-- Get context by glamour level
SELECT code, prompt_modifiers 
FROM face_generation_contexts 
WHERE glamour_level >= 8 AND is_active = true;
*/