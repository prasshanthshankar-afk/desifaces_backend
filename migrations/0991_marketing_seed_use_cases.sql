-- Seed specific use cases (extend to 20–50)

insert into marketing_use_cases (
  use_case_id, persona, industry, recipe, campaign_type, season_event, tags,
  product_anchor, default_offer, default_seconds, default_hook,
  base_overlay_lines, base_script, default_music_prompt, required_assets_json, notes
) values
(
  gen_random_uuid(),
  'smb', 'apparel', 'FACE_CATALOG_PRODUCT_PROMO', 'product_launch', null,
  array['commerce_vton','catalog_reels','apparel'],
  'AI Model Try-On Catalog Reels', 'New arrivals', 10,
  'Turn your catalog into try-on reels.',
  '["AI model try-on from catalog","Reel-ready in minutes","No reshoots required"]'::jsonb,
  'New arrivals? Turn your catalog into a try-on reel. DesiFaces generates model visuals and a promo video—ready to post in minutes.',
  'modern upbeat pop loop, light percussion, 10 seconds',
  '{"needs":["catalog_items"],"optional":["host_face","voiceover_lang"]}'::jsonb,
  'Core VTON promo use case for SMB apparel'
),
(
  gen_random_uuid(),
  'creator', 'general', 'FACE_MUSIC_MUSICVIDEO', 'seasonal', 'holi',
  array['seasonal_holi','creator','music_video'],
  'Holi Festival Reels', null, 10,
  'Holi reels with music + performance.',
  '["Holi vibe • bright colors","Talking performance clip","Music-backed short reel"]'::jsonb,
  'Want Holi content fast? DesiFaces helps you create a short performance reel with voice and music—ready to share today.',
  'festival, high-energy, dhol-inspired loop, 10 seconds',
  '{"needs":["performer_face"],"optional":["language","style_preset"]}'::jsonb,
  'Seasonal Holi creator promo'
);