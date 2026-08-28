-- 0002_paiements.sql — Suivi des paiements automatisés (oth-purchase /
-- oth-payment-hook). Une ligne par tentative, un code d'achat au max.
-- RLS : aucune politique → seul service_role (fonctions) y accède.

create table if not exists public.paiements (
  tx_ref        text primary key,
  plan          text not null,
  email         text not null,
  montant_xof   numeric not null,
  devise        text not null default 'XOF',
  status        text not null default 'en_attente',
  code          text,
  customer      text,
  note          text,
  tx_id         text,
  created_at    timestamptz not null default now(),
  paid_at       timestamptz,
  code_issued_at timestamptz
);

comment on table public.paiements is
  'Paiements Flutterwave : en_attente → paye → emis (code remis) → echec.';

alter table public.paiements enable row level security;

-- Aucune politique : le navigateur ne doit JAMAIS lire cette table.
-- Seules les fonctions Supabase (service_role) la consultent.