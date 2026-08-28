-- ✈️ Activation OmniTradeHub — schéma Supabase
-- Exécuter ce script UNE FOIS dans : Project Settings → SQL Editor → New query
-- (renomme this file schema.sql)

-- ─────────────────────────────────────────────────────────────────────────────
--  Table des codes d'achat (les codes que le vendeur envoie aux clients)
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.purchase_codes (
  id              uuid primary key default gen_random_uuid(),
  code            text not null unique,
  plan            text not null default 'm12',      -- demo7 | m3 | m6 | m12 | life
  days            int  null,                        -- durée (jours) ; null = défaut du plan
  max_activations int  not null default 2,          -- nb d'ordinateurs autorisés
  machines        jsonb not null default '[]'::jsonb, -- [{mid, sn, key, exp, at}]
  created_at      timestamptz not null default now(),
  expires_at      timestamptz null,                 -- date limite d'ACTIVATION du code (null = illimitée)
  revoked         boolean not null default false,
  customer        text null,
  note            text null,
  stripe_session  text null
);

-- Index pour retrouver vite un code envoyé au client
create index if not exists purchase_codes_code_idx on public.purchase_codes (code);
create index if not exists purchase_codes_plan_idx on public.purchase_codes (plan);

-- ─────────────────────────────────────────────────────────────────────────────
--  Sécurité : Row Level Security
--  → le client (clé anon) ne peut RIEN lire ni écrire. Seules les fonctions
--    Edge (clé service_role) touchent cette table.
-- ─────────────────────────────────────────────────────────────────────────────
alter table public.purchase_codes enable row level security;

-- (aucune policy : anon_forbidden par défaut = blocage total côté client)