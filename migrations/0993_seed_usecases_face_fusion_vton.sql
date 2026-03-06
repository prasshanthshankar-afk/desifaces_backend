-- services/svc-marketing/app/app/db/migrations/004_seed_usecases_face_fusion_vton.sql
-- 30 use cases: Face Studio first, then Fusion, then SMB VTON
-- Requires: 003_usecase_evolution.sql applied (approved/source/version columns exist)

insert into marketing_use_cases (
  use_case_id, enabled, approved, source, version,
  weight, persona, industry, recipe, campaign_type, season_event, tags,
  product_anchor, default_offer, default_seconds, default_hook,
  base_overlay_lines, base_script, default_music_prompt,
  required_assets_json, notes
) values

-- -----------------------
-- FACE STUDIO (16)
-- -----------------------

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'creator_branding', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','creator','profile','format_reel'],
  'Creator Profile Photo Pack', null, 10,
  'Upgrade your profile photos in minutes.',
  '["3 pro looks from 1 prompt","Clean skin-tone + lighting","Ready for IG/YouTube"]'::jsonb,
  'Creators: stop recycling the same selfie. DesiFaces Face Studio generates multiple studio-quality profile looks fast—ready to post.',
  null,
  '{"outputs":{"reel":true,"story":true,"carousel":false},"needs":["face"],"optional":["style_preset"]}'::jsonb,
  'Creator profile pack: crisp, premium, non-generic.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'user', 'personal_brand', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','user','linkedin','format_reel'],
  'LinkedIn Headshot Refresh', null, 10,
  'LinkedIn headshot that looks premium.',
  '["Corporate • clean background","Natural skin tones","Recruiter-ready"]'::jsonb,
  'Need a polished LinkedIn headshot? DesiFaces Face Studio generates professional headshots with clean lighting—no photoshoot needed.',
  null,
  '{"outputs":{"reel":true,"story":true},"needs":["face"],"optional":["background_style"]}'::jsonb,
  'LinkedIn/Resume headshot: broad appeal.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'smb', 'services', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','smb','team','format_reel'],
  'SMB Team Headshots', null, 10,
  'Team headshots without a studio.',
  '["Consistent background + style","Brand-ready","Instant gallery"]'::jsonb,
  'Small businesses: standardize your team photos fast. DesiFaces Face Studio creates consistent headshots that match your brand.',
  null,
  '{"outputs":{"reel":true,"story":true},"needs":["face"],"optional":["brand_palette"]}'::jsonb,
  'Perfect SMB wedge before Fusion.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'youtube', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','creator','youtube','thumbnails','format_reel'],
  'YouTube Channel Icon + Thumbnail Face', null, 10,
  'Make a YouTube-ready face in minutes.',
  '["Thumbnail-friendly expression","Crisp edges","Multiple variants"]'::jsonb,
  'YouTubers: get a thumbnail-ready face with the right expression and sharp details—generate variants and pick the best.',
  null,
  '{"outputs":{"reel":true,"story":true},"needs":["face"],"optional":["expression"]}'::jsonb,
  'High-value creator use case.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'user', 'events', 'FACE_AUDIO_VIDEO', 'seasonal', 'wedding',
  array['face_studio','user','wedding','invites','format_story'],
  'Wedding Invite Portrait', null, 8,
  'Wedding invite portrait—done.',
  '["Elegant look","Soft lighting","Invite-ready"]'::jsonb,
  'Need a wedding invite portrait? DesiFaces Face Studio creates an elegant look with soft lighting—ready for your invite design.',
  null,
  '{"outputs":{"reel":false,"story":true,"carousel":true},"needs":["face"],"optional":["attire_style"]}'::jsonb,
  'Seasonal/event content without hard metrics.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'instagram', 'FACE_AUDIO_VIDEO', 'seasonal', 'holi',
  array['face_studio','creator','seasonal_holi','format_reel'],
  'Holi Look Variants', null, 10,
  'Holi looks that pop—instantly.',
  '["Festival colors","Bright lighting","3 looks in 1 run"]'::jsonb,
  'Holi content: generate vibrant festival looks in minutes. DesiFaces Face Studio gives you multiple variants—pick your favorite and post.',
  null,
  '{"outputs":{"reel":true,"story":true},"needs":["face"],"optional":["festival_palette"]}'::jsonb,
  'Seasonal Face Studio proof.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'photography', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','creator','cinematic','format_reel'],
  'Cinematic Portrait', null, 10,
  'Cinematic portrait—no shoot.',
  '["Film lighting","Depth + contrast","Premium vibe"]'::jsonb,
  'Want a cinematic portrait without a photoshoot? DesiFaces Face Studio generates film-style lighting and premium detail—ready to share.',
  null,
  '{"outputs":{"reel":true,"story":true},"needs":["face"],"optional":["lighting_style"]}'::jsonb,
  'Premium “wow” demo.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'travel', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','creator','travel','format_story'],
  'Travel Creator Look', null, 8,
  'Travel look—clean + sharp.',
  '["Outdoor vibe","Natural tones","Story-ready"]'::jsonb,
  'Travel creators: generate a clean outdoor look with natural tones—perfect for stories and reels.',
  null,
  '{"outputs":{"reel":false,"story":true},"needs":["face"],"optional":["scene_hint"]}'::jsonb,
  'Story-friendly Face Studio post.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'user', 'personal', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','user','whatsapp_dp','format_story'],
  'WhatsApp DP Refresh', null, 8,
  'New DP—no effort.',
  '["Clean background","Sharp face","DP-ready crop"]'::jsonb,
  'Refresh your WhatsApp DP with a clean, sharp portrait—generated in minutes with DesiFaces Face Studio.',
  null,
  '{"outputs":{"reel":false,"story":true},"needs":["face"]}'::jsonb,
  'Easy daily content.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'podcast', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','creator','podcast','cover','format_carousel'],
  'Podcast Cover Portrait', null, 10,
  'Podcast cover face—done.',
  '["Cover-ready framing","Clean style","Multiple expressions"]'::jsonb,
  'Podcasters: generate a cover-ready portrait with the right framing and expression—DesiFaces Face Studio gives you options fast.',
  null,
  '{"outputs":{"reel":true,"story":true,"carousel":true},"needs":["face"],"optional":["expression"]}'::jsonb,
  'Great for 1–2 slide carousel.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'smb', 'real_estate', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','smb','realtor','format_reel'],
  'Realtor Headshot', null, 10,
  'Realtor headshot that converts.',
  '["Trustworthy look","Clean background","Professional"]'::jsonb,
  'Realtors: your profile photo matters. DesiFaces Face Studio creates a trustworthy, professional headshot—fast.',
  null,
  '{"outputs":{"reel":true,"story":true},"needs":["face"]}'::jsonb,
  'SMB vertical proof.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'smb', 'salon', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','smb','salon','format_story'],
  'Salon Owner Profile', null, 8,
  'Salon owner photo—premium.',
  '["Warm lighting","Friendly look","Brand-ready"]'::jsonb,
  'Salon owners: show up professionally online. DesiFaces Face Studio generates a premium profile photo with warm lighting.',
  null,
  '{"outputs":{"reel":false,"story":true},"needs":["face"]}'::jsonb,
  'Quick story for local businesses.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'fitness', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','creator','fitness','format_reel'],
  'Fitness Creator Portrait', null, 10,
  'Fitness creator portrait—sharp.',
  '["High-energy look","Clean skin detail","Reel-ready"]'::jsonb,
  'Fitness creators: generate a high-energy portrait with crisp detail—multiple variants in one run.',
  null,
  '{"outputs":{"reel":true,"story":true},"needs":["face"],"optional":["energy_style"]}'::jsonb,
  'Good “creator vertical” expansion.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'user', 'education', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','user','students','resume','format_story'],
  'Student Resume Photo', null, 8,
  'Resume photo—instant.',
  '["Clean crop","Professional tone","Ready to submit"]'::jsonb,
  'Students: get a clean resume photo instantly. DesiFaces Face Studio gives you a professional look—no studio needed.',
  null,
  '{"outputs":{"reel":false,"story":true},"needs":["face"]}'::jsonb,
  'High-volume evergreen.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'branding', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['face_studio','creator','brand_identity','format_carousel'],
  'Brand “Signature Look”', null, 10,
  'Create your signature look.',
  '["Same face • 3 styles","Consistent brand vibe"]'::jsonb,
  'Build a signature look for your brand. DesiFaces Face Studio generates consistent variations so your feed looks cohesive.',
  null,
  '{"outputs":{"reel":true,"story":true,"carousel":true},"needs":["face"],"optional":["brand_style"]}'::jsonb,
  'Use as weekly carousel.'
),

-- -----------------------
-- FUSION STUDIO (10)
-- -----------------------

(gen_random_uuid(), true, true, 'seed', 1,
  1.2, 'creator', 'creator_branding', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['fusion_studio','creator','announcement','format_reel'],
  'Creator Announcement Reel', null, 10,
  'Make an announcement reel—fast.',
  '["Talking video from your avatar","Clear voice","Post-ready"]'::jsonb,
  'Creators: announce new content, launches, or collabs with a short talking reel—generated end-to-end with DesiFaces.',
  null,
  '{"outputs":{"reel":true},"needs":["face","fusion_video"],"optional":["language"]}'::jsonb,
  'Fusion value demo, simple and repeatable.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.2, 'smb', 'services', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['fusion_studio','smb','intro_video','format_reel'],
  'SMB Intro Reel (Salon/Clinic/Store)', null, 10,
  'A store intro reel in minutes.',
  '["Talk → explain → CTA","Professional host","Local business ready"]'::jsonb,
  'Small business owners: generate a short intro reel that explains what you do and invites customers—without filming.',
  null,
  '{"outputs":{"reel":true},"needs":["face","fusion_video"],"optional":["cta_link"]}'::jsonb,
  'Strong SMB conversion use case.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.2, 'smb', 'apparel', 'FACE_AUDIO_VIDEO', 'promo_offer', null,
  array['fusion_studio','smb','offer','format_reel'],
  'Offer Announcement Reel', 'Limited-time offer', 10,
  'Announce your offer with a talking reel.',
  '["Clear offer in 10 seconds","CTA included","Consistent branding"]'::jsonb,
  'Run a flash sale? DesiFaces generates a short talking promo that clearly states the offer and the call-to-action.',
  null,
  '{"outputs":{"reel":true},"needs":["face","fusion_video"],"optional":["brand_palette"]}'::jsonb,
  'Promo offer template.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.1, 'creator', 'education', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['fusion_studio','creator','micro_lesson','format_reel'],
  'Micro-Lesson Reel', null, 10,
  'Teach in 10 seconds.',
  '["One concept • one CTA","Talking head","Repeat weekly"]'::jsonb,
  'Educators and coaches: publish micro-lessons as short talking reels—consistent, fast, and professional.',
  null,
  '{"outputs":{"reel":true},"needs":["face","fusion_video"],"optional":["topic"]}'::jsonb,
  'Great weekly series idea.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.1, 'creator', 'multilingual', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['fusion_studio','creator','multilingual','format_story'],
  'Multilingual Greeting Story', null, 8,
  'Say it in your language.',
  '["Short greeting","Clear voice","Story-ready"]'::jsonb,
  'Create short greetings in your language for stories—DesiFaces generates the talking clip quickly.',
  null,
  '{"outputs":{"story":true},"needs":["face","fusion_video"],"optional":["language"]}'::jsonb,
  'Quick story content.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.1, 'smb', 'customer_support', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['fusion_studio','smb','faq','format_reel'],
  'FAQ Reel (“How to order?”)', null, 10,
  'Answer FAQs without filming.',
  '["One FAQ per reel","Clear steps","Less support load"]'::jsonb,
  'Businesses: publish one FAQ reel at a time—how to order, delivery, returns—without filming a single video.',
  null,
  '{"outputs":{"reel":true},"needs":["face","fusion_video"],"optional":["faq_text"]}'::jsonb,
  'Business utility content.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'testimonials', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['fusion_studio','creator','testimonial','format_carousel'],
  'Testimonial + Talking Clip', null, 10,
  'Turn testimonials into content.',
  '["Talk + quote overlay","Proof-driven","Post-ready"]'::jsonb,
  'Turn a customer quote into a short talking clip with a clean overlay—professional proof content.',
  null,
  '{"outputs":{"reel":true,"carousel":true},"needs":["face","fusion_video"],"optional":["quote_text"]}'::jsonb,
  'Works as 1–2 slide carousel too.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'smb', 'recruiting', 'FACE_AUDIO_VIDEO', 'evergreen', null,
  array['fusion_studio','smb','hiring','format_reel'],
  'Hiring Reel', null, 10,
  'Hiring reel—fast.',
  '["Role + location + CTA","Professional host"]'::jsonb,
  'Hiring? Create a short reel that announces the role and how to apply—without filming.',
  null,
  '{"outputs":{"reel":true},"needs":["face","fusion_video"],"optional":["role"]}'::jsonb,
  'SMB hiring is always relevant.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'creator', 'events', 'FACE_AUDIO_VIDEO', 'seasonal', 'event',
  array['fusion_studio','creator','event_invite','format_story'],
  'Event Invite Story', null, 8,
  'Invite people—on story.',
  '["Short invite","Date + CTA","Story-ready"]'::jsonb,
  'Create an event invite story with a short talking clip—date, location, and CTA—done.',
  null,
  '{"outputs":{"story":true},"needs":["face","fusion_video"],"optional":["event_details"]}'::jsonb,
  'Weekly story driver.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.0, 'smb', 'product', 'FACE_AUDIO_VIDEO', 'product_launch', null,
  array['fusion_studio','smb','product_explainer','format_reel'],
  'Product Feature Explainer Reel', null, 10,
  'Explain one feature in 10 seconds.',
  '["One feature","One benefit","One CTA"]'::jsonb,
  'Launch a product? Generate a short talking reel that explains one feature and one benefit—clear and effective.',
  null,
  '{"outputs":{"reel":true},"needs":["face","fusion_video"],"optional":["product_details"]}'::jsonb,
  'Great for SMB funnels.'
),

-- -----------------------
-- SMB APPAREL + VTON TRY-ON (4) (seed now, schedule later)
-- -----------------------

(gen_random_uuid(), true, true, 'seed', 1,
  1.4, 'smb', 'apparel', 'FACE_CATALOG_PRODUCT_PROMO', 'product_launch', null,
  array['commerce','vton','catalog_reels','smb','format_reel'],
  'AI Model Try-On Catalog Reels', 'New arrivals', 10,
  'Turn your catalog into try-on reels.',
  '["AI model try-on from catalog","Reel-ready in minutes","No reshoots required"]'::jsonb,
  'New arrivals? Turn catalog images into an AI try-on reel. DesiFaces generates model visuals and a promo video—ready to post.',
  null,
  '{"outputs":{"reel":true},"needs":["catalog_items","tryon"],"optional":["offer","brand_palette"]}'::jsonb,
  'Core VTON SMB wedge.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.3, 'smb', 'apparel', 'FACE_CATALOG_PRODUCT_PROMO', 'promo_offer', 'winter_sale',
  array['commerce','vton','seasonal_winter','sale','format_reel'],
  'Winter Sale Lookbook Reel', 'Winter sale', 10,
  'Winter sale lookbook—auto.',
  '["Sale looks on model","Catalog → Reel","CTA included"]'::jsonb,
  'Winter sale? Generate a short lookbook reel from your catalog with try-on visuals—ready to post with a clear CTA.',
  null,
  '{"outputs":{"reel":true},"needs":["catalog_items","tryon"],"optional":["offer"]}'::jsonb,
  'Seasonal commerce campaign.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.3, 'smb', 'apparel', 'FACE_CATALOG_PRODUCT_PROMO', 'product_launch', null,
  array['commerce','vton','saree','format_reel'],
  'Saree Set Try-On Reel', 'New drop', 10,
  'Saree try-on reel—catalog powered.',
  '["Saree drape focus","Multiple variants","Promo-ready"]'::jsonb,
  'Saree sellers: generate a try-on reel focusing on drape and look. DesiFaces turns catalog inputs into a promo video.',
  null,
  '{"outputs":{"reel":true},"needs":["catalog_items","tryon_saree"],"optional":["drape_style"]}'::jsonb,
  'VTON demo once ready.'
),

(gen_random_uuid(), true, true, 'seed', 1,
  1.2, 'smb', 'apparel', 'FACE_CATALOG_PRODUCT_PROMO', 'evergreen', null,
  array['commerce','vton','mix_match','format_carousel'],
  'Mix & Match Outfit Carousel', null, 10,
  'Mix & match—show options.',
  '["Top+bottom combos","Carousel-ready","Catalog-driven"]'::jsonb,
  'Show multiple outfit combinations from your catalog as a short carousel-style promo—consistent and fast.',
  null,
  '{"outputs":{"carousel":true,"reel":false},"needs":["catalog_items"],"optional":["bundle_rules"]}'::jsonb,
  'Good for 1–2 slide weekly carousel.'
);