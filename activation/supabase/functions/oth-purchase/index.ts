// oth-purchase — ENTRÉE DU TUNNEL DE VENTE AUTOMATISÉ.
//
// Le client (page « vente/index.html ») choisit un plan + saisit son e-mail.
// Cette fonction crée la transaction Flutterwave (Standard checkout) et lui
// renvoie le lien de paiement. Quand Flutterwave paye, il appelle
// « oth-payment-hook » (webhook + redirection) qui remet le code d'achat.
//
// DÉPLOIEMENT : supabase functions deploy oth-purchase --no-verify-jwt
// SECRETS :     FLW_SECRET_KEY   (clé secrète de votre compte Flutterwave)
//               + SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (injectés)
//
// Appel (from la page de vente) :
//   POST https://<ref>.supabase.co/functions/v1/oth-purchase
//   headers : { Authorization: 'Bearer <ANON_KEY>', 'Content-Type': 'application/json' }
//   body    : { plan: 'm12', email: 'client@mail.com', prenom?: 'Nom' }
// Retour   : { ok:true, link:'https://checkout.flutterwave.com/…', tx_ref }
//            ou { ok:false, code:'paiement_non_configure' } tant que
//            FLW_SECRET_KEY n'est pas posé.
import { createClient, SupabaseClient } from 'https://esm.sh/@supabase/supabase-js@2'
import { PAYABLE, DAYS, DEVISE } from '../_shared/pricing.ts'

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

const B32A = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'

function randB32(nBytes: number): string {
  const b = new Uint8Array(nBytes)
  globalThis.crypto.getRandomValues(b)
  let bits = ''
  for (const x of b) bits += x.toString(2).padStart(8, '0')
  let out = ''
  for (let i = 0; i < bits.length; i += 5) out += B32A[parseInt(bits.slice(i, i + 5).padEnd(5, '0'), 2)]
  return out
}

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/

export async function handler(req: Request, ctx?: { env?: Record<string, string> }): Promise<Response> {
  if (req.method === 'OPTIONS') return new Response('ok', { status: 204, headers: cors })

  const env = ctx?.env ?? (Deno as any).env.toObject?.() ?? {}
  const supabaseUrl = env.SUPABASE_URL || ''
  const serviceKey = env.SUPABASE_SERVICE_ROLE_KEY || ''
  const flwSecret = env.FLW_SECRET_KEY || ''
  if (!supabaseUrl || !serviceKey) {
    return json(500, { ok: false, code: 'configuration', msg_fr: 'Paiement mal configuré (serveur).' })
  }
  if (!flwSecret) {
    return json(503, {
      ok: false,
      code: 'paiement_non_configure',
      msg_fr: 'Le paiement en ligne est en cours de mise en place — contactez le vendeur.',
      msg_en: 'Online payment is being set up — please contact the seller.',
    })
  }

  let body: { plan?: string; email?: string; prenom?: string }
  try {
    body = await req.json()
  } catch {
    return json(400, { ok: false, code: 'json', msg_fr: 'Requête invalide.' })
  }

  const plan = (body.plan || '').toLowerCase()
  if (!PAYABLE[plan]) {
    return json(400, {
      ok: false,
      code: 'plan_inconnu',
      msg_fr: 'Plan inconnu. Choisissez 3, 6, 12 mois ou la licence à vie.',
    })
  }
  const email = (body.email || '').trim().toLowerCase()
  if (!EMAIL_RE.test(email)) {
    return json(400, { ok: false, code: 'email_invalide', msg_fr: 'E-mail invalide.' })
  }

  const montant = PAYABLE[plan]
  const tx_ref = 'OTH-' + randB32(10)

  const supabase: SupabaseClient = createClient(supabaseUrl, serviceKey)

  // 1 ── tracer la tentative avant même le paiement (un seul code par tx)
  const { error: eIns } = await supabase.from('paiements').insert({
    tx_ref,
    plan,
    email,
    montant_xof: montant,
    devise: DEVISE,
    status: 'en_attente',
    customer: body.prenom || null,
  })
  if (eIns) return json(500, { ok: false, code: 'technique', msg_fr: 'Impossible de démarrer le paiement.' })

  // 2 ── ouvrir le checkout Flutterwave (Standard, page hébergée)
  const redirectUrl = supabaseUrl + '/functions/v1/oth-payment-hook'
  try {
    const fr = await fetch('https://api.flutterwave.com/v3/payments', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + flwSecret,
      },
      body: JSON.stringify({
        tx_ref,
        amount: montant,
        currency: DEVISE,
        redirect_url: redirectUrl,
        customer: { email, name: body.prenom || 'Client OmniTrade' },
        meta: { plan, email },
        customizations: {
          title: 'OmniTrade Hub',
          description: 'Licence ' + plan.toUpperCase() + ' — ' + DAYS[plan] + ' jours',
        },
      }),
    })
    const fj = await fr.json().catch(() => null)
    if (!fr.ok || !fj || !fj.data || !fj.data.link) {
      await supabase.from('paiements').update({ status: 'echec', note: 'flw:' + fr.status }).eq('tx_ref', tx_ref)
      return json(502, {
        ok: false,
        code: 'flw_refuse',
        msg_fr: 'Le prestataire de paiement a refusé la demande. Recommencez ou contactez le vendeur.',
      })
    }
    return json(200, { ok: true, link: fj.data.link, tx_ref, montant, devise: DEVISE, plan })
  } catch {
    await supabase.from('paiements').update({ status: 'echec', note: 'flw:req' }).eq('tx_ref', tx_ref)
    return json(502, { ok: false, code: 'flw_injoignable', msg_fr: 'Prestataire de paiement injoignable. Réessayez.' })
  }
}

Deno.serve(handler)