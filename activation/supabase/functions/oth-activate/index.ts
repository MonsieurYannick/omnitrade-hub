// oth-activate — ACTIVATION PAR CODE D'ACHAT + COMPTE E-MAIL
//
// Modèle v9 : le code d'achat couplé à l'e-mail (le compte de l'acheteur)
// + l'identifiant LOGICIEL de l'appareil (soft ID, persistant dans le profil
// utilisateur). Explanément : l'identifiant survit à la réinstallation, et
// l'e-mail permet de voir / retirer ses appareils soi-même.
//
// DÉPLOIEMENT :  supabase functions deploy oth-activate --no-verify-jwt
// SECRETS :      OTH_PRIV_KEY   → hex de la clé PRIVÉE Ed25519 (32 octets)
//                SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY → injectés par Supabase
//
// Appel (from l'app) : POST https://<ref>.supabase.co/functions/v1/oth-activate
//   headers : { Authorization: 'Bearer <ANON_KEY>', 'Content-Type': 'application/json' }
//
//   action 'activate' (défaut) :
//     body : { code:'OTH-XXXXXXXX-XXXXXXXX', email:'a@b.c', machine_id:'MBK7…' }
//     → { ok:true, key, plan, expires, sn, devices }
//   action 'list' :
//     body : { code, email }
//     → { ok:true, devices:[{mid, email, plan, expires, sn, at}] }
//   action 'remove' :
//     body : { code, email, machine_id }
//     → { ok:true, removed:true, devices }
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

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/

const ERR_MSG: Record<string, { fr: string; en: string }> = {
  code_vide: {
    fr: 'Collez votre code d\u2019achat (ex : OTH-M5F3XQ2P-RTLK92U).',
    en: 'Paste your purchase code (e.g. OTH-M5F3XQ2P-RTLK92U).',
  },
  email_invalide: {
    fr: 'Saisissez l\u2019e-mail utilisé lors de l\u2019achat (ex : trader@mail.com).',
    en: 'Enter the e-mail used at purchase (e.g. trader@mail.com).',
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
  email_mismatch: {
    fr: 'Cet e-mail ne correspond pas à l\u2019achat de ce code. Utilisez l\u2019e-mail de commande.',
    en: 'This e-mail does not match the purchase. Use the order e-mail.',
  },
  plan_inconnu: {
    fr: 'Plan inconnu, contactez le vendeur.',
    en: 'Unknown plan, contact the seller.',
  },
  limite_atteinte: {
    fr: 'Ce code a déjà été utilisé sur le nombre maximum d\u2019appareils. Retirez un appareil pour libérer une place.',
    en: 'This code reached its maximum number of devices. Remove one to free a slot.',
  },
  appareil_inconnu: {
    fr: 'Cet appareil n\u2019est pas enregistré sur ce code.',
    en: 'This device is not registered on this code.',
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
  email?: string
  sn: string
  key: string
  exp: string
  plan: string
  at: string
}

function cleanEmail(raw: string | undefined): string {
  return (raw || '').trim().toLowerCase()
}

/* purchase_codes.machines est un jsonb. PostgREST renvoie un tableau déjà
   parsé, MAIS si une écriture antérieure a stocké une chaîne JSON, on la
   reçoit en texte : on normalise pour ne jamais avoir besoin d'un tableau
   vide à cause d'un stockage en chaîne. */
function parseMachines(raw: unknown): MachineEntry[] {
  if (Array.isArray(raw)) return raw as MachineEntry[]
  if (typeof raw === 'string' && raw.trim()) {
    try {
      const v = JSON.parse(raw)
      return Array.isArray(v) ? (v as MachineEntry[]) : []
    } catch {
      return []
    }
  }
  return []
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

  let body: {
    code?: string
    email?: string
    machine_id?: string
    action?: string
  }
  try {
    body = await req.json()
  } catch {
    return json(400, { ok: false, code: 'json', msg_fr: 'Requête invalide.', msg_en: 'Invalid request.' })
  }

  // Normalisation tolérante du code : tirets et espaces acceptés, avec ou sans préfixe.
  const cLoose = (body.code || '').trim().toUpperCase().replace(/\s+/g, '').replace(/-/g, '')
  const codeRaw = cLoose.startsWith('OTH') && cLoose.length >= 19
    ? 'OTH-' + cLoose.slice(3, 11) + '-' + cLoose.slice(11, 19)
    : (body.code || '').trim().toUpperCase().replace(/\s+/g, '')
  if (!codeRaw) return err('code_vide')

  const action = (body.action || 'activate').trim().toLowerCase()
  const email = cleanEmail(body.email)
  if (!EMAIL_RE.test(email)) return err('email_invalide')

  const supabase: SupabaseClient = createClient(supabaseUrl, serviceKey)

  try {
    // 1 ── retrouver le code d'achat
    const { data: row, error: eSel } = await supabase
      .from('purchase_codes')
      .select('code, plan, days, max_activations, machines, expires_at, revoked, customer')
      .eq('code', codeRaw)
      .maybeSingle()
    if (eSel) return err('technique')
    if (!row) return err('code_inconnu')
    if (row.revoked) return err('code_revoque')
    if (row.expires_at && new Date(row.expires_at).getTime() < Date.now()) return err('code_expire')

    const machines = parseMachines(row.machines)
    const purchEmail = cleanEmail(row.customer)

    // 2 ── contrôle e-mail : le compte doit correspondre à l'achat
    if (purchEmail && purchEmail !== email) return err('email_mismatch')

    // ── LISTE DES APPAREILS ──
    if (action === 'list') {
      return json(200, {
        ok: true,
        devices: machines.map((m) => ({
          mid: m.mid,
          plan: m.plan,
          expires: m.exp,
          sn: m.sn,
          at: m.at,
        })),
      })
    }

    // ── RETRAIT D'UN APPAREIL ──
    if (action === 'remove') {
      const mid = normalizeMid(body.machine_id || '')
      if (!mid) return err('machine_vide')
      const rest = machines.filter((m) => normalizeMid(m.mid) !== mid)
      if (rest.length === machines.length) return err('appareil_inconnu')
      const { error: eUpd } = await supabase
        .from('purchase_codes')
        .update({ machines: rest })
        .eq('code', codeRaw)
      if (eUpd) return err('technique')
      return json(200, {
        ok: true,
        removed: true,
        devices: rest.map((m) => ({ mid: m.mid, plan: m.plan, expires: m.exp })),
      })
    }

    // ── ACTIVATION (valeur par défaut, ou 'activate') ──
    const mid = normalizeMid(body.machine_id || '')
    if (!mid) return err('machine_vide')

    // 3 ── déjà activé sur CET appareil ? → renvoyer la même clé (idempotent)
    const existing = machines.find((m) => normalizeMid(m.mid) === mid)
    if (existing) {
      return json(200, {
        ok: true,
        deja_utilise: true,
        key: existing.key,
        plan: existing.plan,
        expires: existing.exp,
        sn: existing.sn,
        devices: machines.length,
      })
    }

    // 4 ── limite d'appareils atteinte ?
    if (machines.length >= Number(row.max_activations)) return err('limite_atteinte')

    // 5 ── durée : jours du code (> défaut du plan)
    const plan = row.plan
    const days = row.days !== null && row.days !== undefined ? Number(row.days) : (PLANS[plan] ?? null)
    if (!(plan in PLANS)) return err('plan_inconnu')

    const { key, payload } = await makeLicense(skHex, plan, days, mid, 1)
    const entry: MachineEntry = {
      mid,
      email,
      sn: payload.sn,
      key,
      exp: payload.exp,
      plan,
      at: new Date().toISOString(),
    }
    const { error: eUpd } = await supabase
      .from('purchase_codes')
      .update({
        machines: [...machines, entry],
        customer: purchEmail || email,
      })
      .eq('code', codeRaw)
    if (eUpd) return err('technique')

    return json(200, {
      ok: true,
      key,
      plan,
      expires: payload.exp,
      sn: payload.sn,
      devices: machines.length + 1,
    })
  } catch {
    return err('technique')
  }
}

Deno.serve(handler)