// oth-activate — ActivA UN CODE D'ACHAT : émet une licence signée liée au
// code machine du client.
//
// DÉPLOIEMENT :  supabase functions deploy oth-activate --no-verify-jwt
// SECRETS :      OTH_PRIV_KEY   → hex de la clé PRIVÉE Ed25519 (32 octets)
//                SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY → injectés par Supabase
//
// Appel (from l'app) : POST https://<ref>.supabase.co/functions/v1/oth-activate
//   headers : { Authorization: 'Bearer <ANON_KEY>', 'Content-Type': 'application/json' }
//   body    : { code: 'OTH-XXXXXXXX-XXXXXXXX', machine_id: 'MBK7UVEURZDSD35Z' }
// Retour (ok) : { ok:true, key:'OTH1-…-…', plan, expires, sn }
// Retour (ko) : { ok:false, code:'permalink' }
import { createClient, SupabaseClient } from 'https://esm.sh/@supabase/supabase-js@2'
import { makeLicense, normalizeMid, PLANS } from '../_shared/oth_core.ts'

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type',
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...cors },
  })
}

const ERR_MSG: Record<string, { fr: string; en: string }> = {
  code_vide: {
    fr: 'Collez votre code d\u2019achat (ex : OTH-M5F3XQ2P-RTLK92U).',
    en: 'Paste your purchase code (e.g. OTH-M5F3XQ2P-RTLK92U).',
  },
  machine_vide: {
    fr: 'Le moteur est en démarrage, ressayez dans quelques secondes.',
    en: 'The engine is still starting, retry in a few seconds.',
  },
  code_inconnu: {
    fr: 'Ce code d\u2019achat est inconnu. Vérifiez le code (espaces, tirets, lettre O / chiffre 0).',
    en: 'Unknown purchase code. Check spaces, dashes, letter O / digit 0.',
  },
  code_expire: {
    fr: 'Ce code d\u2019achat a expiré. Contactez le vendeur.',
    en: 'This purchase code has expired. Contact the seller.',
  },
  code_revoque: {
    fr: 'Ce code d\u2019achat a été révoqué. Contactez le vendeur.',
    en: 'This purchase code has been revoked. Contact the seller.',
  },
  plan_inconnu: {
    fr: 'Plan inconnu, combustion le vendeur.',
    en: 'Unknown plan, contact the seller.',
  },
  limite_atteinte: {
    fr: 'Ce code a déjà été utilisé sur le nombre maximum d\u2019ordinateurs.',
    en: 'This code has reached its maximum number of activations.',
  },
  technique: {
    fr: 'Erreur technique. Ressayez dans quelques instants.',
    en: 'Technical error. Retry in a few moments.',
  },
}

function err(code: string): Response {
  const m = ERR_MSG[code] ?? ERR_MSG.technique
  return json(400, { ok: false, code, msg_fr: m.fr, msg_en: m.en })
}

interface MachineEntry {
  mid: string
  sn: string
  key: string
  exp: string
  plan: string
  at: string
}

export async function handler(req: Request, ctx: { env?: Record<string, string> }): Promise<Response> {
  if (req.method === 'OPTIONS') return new Response('ok', { status: 204, headers: cors })

  const env = ctx.env ?? (Deno as any).env.toObject?.() ?? {}
  const supabaseUrl = env.SUPABASE_URL || ''
  const serviceKey = env.SUPABASE_SERVICE_ROLE_KEY || ''
  const skHex = env.OTH_PRIV_KEY || ''
  if (!supabaseUrl || !serviceKey || !skHex) {
    return json(500, {
      ok: false,
      code: 'configuration',
      msg_fr: 'Activation non configurée (secrets manquants).',
      msg_en: 'Activation not configured (missing secrets).',
    })
  }

  let body: { code?: string; machine_id?: string }
  try {
    body = await req.json()
  } catch {
    return json(400, { ok: false, code: 'json', msg_fr: 'Requête invalide.', msg_en: 'Invalid request.' })
  }

  // Normalisation tolérante : tirets et espaces acceptés, avec ou sans préfixe.
  // Stockage canonique : OTH-XXXXXXXX-XXXXXXXX (voir oth-issue/newCode).
  const cLoose = (body.code || '').trim().toUpperCase().replace(/\s+/g, '').replace(/-/g, '')
  const codeRaw = cLoose.startsWith('OTH') && cLoose.length >= 19
    ? 'OTH-' + cLoose.slice(3, 11) + '-' + cLoose.slice(11, 19)
    : (body.code || '').trim().toUpperCase().replace(/\s+/g, '')
  if (!codeRaw) return err('code_vide')

  const mid = normalizeMid(body.machine_id || '')
  if (!mid) return err('machine_vide')

  const supabase: SupabaseClient = createClient(supabaseUrl, serviceKey)

  try {
    // 1 ── retrouver le code d'achat
    const { data: row, error: eSel } = await supabase
      .from('purchase_codes')
      .select('code, plan, days, max_activations, machines, expires_at, revoked')
      .eq('code', codeRaw)
      .maybeSingle()
    if (eSel) return err('technique')
    if (!row) return err('code_inconnu')
    if (row.revoked) return err('code_revoque')
    if (row.expires_at && new Date(row.expires_at).getTime() < Date.now()) return err('code_expire')

    const machines: MachineEntry[] = Array.isArray(row.machines) ? row.machines : []

    // 2 ── déjà activé sur CET ordinateur ? → renvoyer la même clé (idempotent)
    const existing = machines.find((m) => normalizeMid(m.mid) === mid)
    if (existing) {
      return json(200, {
        ok: true,
        deja_utilise: true,
        key: existing.key,
        plan: existing.plan,
        expires: existing.exp,
        sn: existing.sn,
      })
    }

    // 3 ── limite d'ordinateurs atteinte ?
    if (machines.length >= Number(row.max_activations)) return err('limite_atteinte')

    // 4 ── durée : jours du code (> défaut du plan)
    const plan = row.plan
    const days = row.days !== null && row.days !== undefined ? Number(row.days) : (PLANS[plan] ?? null)
    if (!(plan in PLANS)) return err('plan_inconnu')

    const { key, payload } = await makeLicense(skHex, plan, days, mid, 1)
    const entry: MachineEntry = {
      mid,
      sn: payload.sn,
      key,
      exp: payload.exp,
      plan,
      at: new Date().toISOString(),
    }
    const { error: eUpd } = await supabase
      .from('purchase_codes')
      .update({ machines: JSON.stringify([...machines, entry]) })
      .eq('code', codeRaw)
    if (eUpd) return err('technique')

    return json(200, {
      ok: true,
      key,
      plan,
      expires: payload.exp,
      sn: payload.sn,
    })
  } catch {
    return err('technique')
  }
}

Deno.serve(handler)