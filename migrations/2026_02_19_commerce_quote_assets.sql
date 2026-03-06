-- 1) Catalog of component types (DB-driven; vendors can map their types to these codes)
create table if not exists public.commerce_garment_components (
  id uuid primary key default gen_random_uuid(),
  code text not null unique,                       -- e.g. "saree", "blouse", "shirt", "pants"
  display_name text not null,
  kind text not null check (kind in ('garment','accessory','jewelry','footwear','other')),
  is_accessory boolean not null default false,
  dominance_rank int not null default 100,         -- smaller = more dominant
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_commerce_garment_components_kind
  on public.commerce_garment_components(kind);

-- Optional: valid combinations (saree+blouse, suit=shirt+pants, etc.)
create table if not exists public.commerce_component_combinations (
  id uuid primary key default gen_random_uuid(),
  combo_code text not null unique,                 -- e.g. "saree_blouse"
  display_name text not null,
  primary_component_code text not null references public.commerce_garment_components(code),
  component_codes text[] not null,                 -- e.g. {"saree","blouse"}
  constraints_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- 2) Persist full request + resolved fields (adapt table name if yours differs)
alter table if exists public.commerce_quotes
  add column if not exists request_json jsonb,
  add column if not exists resolved_json jsonb,
  add column if not exists mode text,
  add column if not exists resolution text,
  add column if not exists dominant_component_code text,
  add column if not exists resolved_garment_image_url text,
  add column if not exists resolved_human_image_url text;

create index if not exists idx_commerce_quotes_mode on public.commerce_quotes(mode);
create index if not exists idx_commerce_quotes_resolution on public.commerce_quotes(resolution);

-- 3) Seed a minimal catalog (safe upsert)
insert into public.commerce_garment_components(code, display_name, kind, is_accessory, dominance_rank)
values
  ('saree','Saree','garment',false,10),
  ('blouse','Blouse','garment',false,20),
  ('shirt','Shirt','garment',false,20),
  ('pants','Pants','garment',false,30),
  ('dress','Dress','garment',false,10),
  ('jewelry_necklace','Necklace','jewelry',true,200),
  ('jewelry_earrings','Earrings','jewelry',true,200),
  ('handbag','Handbag','accessory',true,200),
  ('shoes','Shoes','footwear',true,150)
on conflict (code) do update set
  display_name = excluded.display_name,
  kind = excluded.kind,
  is_accessory = excluded.is_accessory,
  dominance_rank = excluded.dominance_rank,
  updated_at = now();