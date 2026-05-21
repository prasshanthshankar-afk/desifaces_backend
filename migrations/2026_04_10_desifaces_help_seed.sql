INSERT INTO help_categories (key, title, description, sort_order, is_active)
VALUES
  ('getting_started', 'Getting started', 'Start with Face, then Audio, then Fusion.', 10, TRUE),
  ('billing', 'Plans & billing', 'Understand pricing, subscriptions, and usage.', 20, TRUE),
  ('face', 'Face Studio', 'Create and edit face assets.', 30, TRUE),
  ('audio', 'Audio Studio', 'Voice, speech, and TTS help.', 40, TRUE),
  ('fusion', 'Fusion Studio', 'Video creation and output help.', 50, TRUE),
  ('account', 'Account & support', 'Support, account, and general help.', 60, TRUE)
ON CONFLICT (key) DO UPDATE
SET
  title = EXCLUDED.title,
  description = EXCLUDED.description,
  sort_order = EXCLUDED.sort_order,
  is_active = EXCLUDED.is_active;

INSERT INTO help_articles
  (slug, category_key, title, summary, body_markdown, keywords, is_faq, is_published, sort_order)
VALUES
  (
    'what-is-desifaces',
    'getting_started',
    'What is DesiFaces.ai?',
    'Overview of the DesiFaces platform.',
    '# What is DesiFaces.ai?

DesiFaces.ai helps creators, founders, professionals, and communities create culturally resonant Face, Audio, and Fusion content with a premium workflow.',
    ARRAY['desifaces', 'overview', 'platform'],
    TRUE,
    TRUE,
    10
  ),
  (
    'how-to-start',
    'getting_started',
    'How do I start?',
    'Recommended first workflow.',
    '# How do I start?

Begin with Face Studio, continue to Audio Studio, and then move to Fusion Studio for video generation.',
    ARRAY['start', 'workflow', 'face', 'audio', 'fusion'],
    TRUE,
    TRUE,
    20
  ),
  (
    'pricing-preview-required',
    'billing',
    'Why do I need pricing preview before generate?',
    'Why preview exists.',
    '# Why do I need pricing preview?

Pricing preview gives you the expected charge before a run starts. This improves trust and prevents billing surprises.',
    ARRAY['pricing', 'preview', 'billing'],
    TRUE,
    TRUE,
    10
  ),
  (
    'fusion-processing-time',
    'fusion',
    'Why is my Fusion job still processing?',
    'Video jobs take longer.',
    '# Why is my Fusion job still processing?

Fusion involves orchestration, provider processing, composition, and final output generation, so jobs may take longer than image or audio runs.',
    ARRAY['fusion', 'processing', 'video'],
    TRUE,
    TRUE,
    10
  ),
  (
    'contact-support',
    'account',
    'How do I contact support?',
    'Support guidance.',
    '# How do I contact support?

Open the Contact Us screen, choose the correct topic and product area, and submit your message. You will receive an acknowledgement email.',
    ARRAY['support', 'contact', 'help'],
    TRUE,
    TRUE,
    10
  )
ON CONFLICT (slug) DO UPDATE
SET
  category_key = EXCLUDED.category_key,
  title = EXCLUDED.title,
  summary = EXCLUDED.summary,
  body_markdown = EXCLUDED.body_markdown,
  keywords = EXCLUDED.keywords,
  is_faq = EXCLUDED.is_faq,
  is_published = EXCLUDED.is_published,
  sort_order = EXCLUDED.sort_order,
  updated_at = now();