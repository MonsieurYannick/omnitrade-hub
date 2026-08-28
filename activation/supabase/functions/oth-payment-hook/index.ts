// oth-payment-hook — TERMINAISON DU TUNNEL DE VENTE AUTOMATISÉ.
//
// Deux entrées :
//  • POST   → webhook Flutterwave (serveur → serveur). Signature validée par
//             le header « verif-hash », paiement re-vérifié sur l'API FLW,
//             puis code d'achat créé via oth-issue et enregistré.
//  • GET    → page de retour après paiement (Flutterwave redirige le client
//             ici) : état vérifié + code affiché au client, dès qu'il existe.
//
// DÉPLOIEMENT : supabase functions deploy oth-payment-hook --no-verify-jwt
// SECRETS :     FLW_SECRET_KEY   (clé secrète Flutterwave)
//               FLW_VERIF_HASH   (le « webhook secret » défini dans votre
//                                 tableau de bord Flutterwave → Webhooks)
//               OTH_ADMIN_KEY    (pour appeler oth-issue en interne)
//
// Garantie d'honnêteté : aucun code n'est remis sans que l'API Flutterwave
// confirme elle-même le succès du paiement ET que le montant payé soit bien
// celui du plan (oth-purchase/pricing.ts).
import { createClient, SupabaseClient } from 'https://esm.sh/@supabase/supabase-js@2'
import { PAYABLE, DAYS, DEVISE } from '../_shared/pricing.ts'

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type, verif-hash',
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...cors },
  })
}

function html(body: string, code = 200): Response {
  return new Response(body, { status: code, headers: { 'Content-Type': 'text/html; charset=utf-8', ...cors } })
}

export async function handler(req: Request, ctx?: { env?: Record<string, string> }): Promise<Response> {
  if (req.method === 'OPTIONS') return new Response('ok', { status: 204, headers: cors })

  const env = ctx?.env ?? (Deno as any).env.toObject?.() ?? {}
  const supabaseUrl = env.SUPABASE_URL || ''
  const serviceKey = env.SUPABASE_SERVICE_ROLE_KEY || ''
  const flwSecret = env.FLW_SECRET_KEY || ''
  const verifHash = env.FLW_VERIF_HASH || ''
  const adminKey = env.OTH_ADMIN_KEY || ''
  if (!supabaseUrl || !serviceKey || !adminKey) {
    return json(500, { ok: false, code: 'configuration' })
  }

  const supabase: SupabaseClient = createClient(supabaseUrl, serviceKey)

  // ── vérification du paiement auprès de Flutterwave ───────────────────────
  async function verifyTx(txId: number | string): Promise<{ ok: boolean; amount?: number; cur?: string } | null> {
    try {
      const r = await fetch('https://api.flutterwave.com/v3/transactions/' + txId + '/verify', {
        headers: { Authorization: 'Bearer ' + flwSecret },
      })
      const j = await r.json().catch(() => null)
      if (!r.ok || !j || !j.data) return { ok: false }
      const d = j.data
      return {
        ok: d.status === 'successful',
        amount: Number(d.amount),
        cur: String(d.currency || '').toUpperCase(),
      }
    } catch {
      return null
    }
  }

  // ── création IDEMPOTENTE du code d'achat (via oth-issue) ────────────────
  async function issueCode(txRef: string, plan: string, email: string, txId: string): Promise<string | null> {
    const { data: row } = await supabase.from('paiements').select('*').eq('tx_ref', txRef).maybeSingle()
    if (!row) return null
    if (row.code) return row.code // déjà émis → renvoyer le même code

    const expected = PAYABLE[plan]
    const v = await verifyTx(txId)
    if (!v || !v.ok || v.amount !== expected || v.cur !== DEVISE) {
      await supabase.from('paiements').update({ status: 'echec', tx_id: String(txId) }).eq('tx_ref', txRef)
      return null
    }

    try {
      const admin = await fetch(supabaseUrl + '/functions/v1/oth-issue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-oth-admin': adminKey },
        body: JSON.stringify({
          action: 'create',
          plan,
          days: DAYS[plan],
          max_activations: 2,
          customer: email,
          note: 'Paiement automatique ' + txRef,
        }),
      })
      const aj = await admin.json().catch(() => null)
      if (!admin.ok || !aj || !aj.ok || !aj.code) return null

      await supabase
        .from('paiements')
        .update({
          status: 'emis',
          code: aj.code,
          tx_id: String(txId),
          paid_at: new Date().toISOString(),
          code_issued_at: new Date().toISOString(),
        })
        .eq('tx_ref', txRef)
      return aj.code
    } catch {
      return null
    }
  }

  // ── page de retour (GET) : le client revient du checkout ────────────────
  if (req.method === 'GET') {
    const url = new URL(req.url)
    const status = url.searchParams.get('status') || ''
    const txRef = (url.searchParams.get('tx_ref') || '').toUpperCase()
    const txId = url.searchParams.get('tx_id') || ''
    const plan = url.searchParams.get('plan') || ''

    const style =
      'body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#0b1220;color:#e2e8f0;'
      + 'margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;}'
      + '.box{max-width:520px;width:92%;background:#111a2e;border:1px solid #26334d;border-radius:14px;'
      + 'padding:30px 26px;text-align:center;box-shadow:0 10px 30px rgba(0,0,0,.4);}'
      + 'h1{font-size:19px;margin:0 0 6px;color:#fff;}'
      + 'p{font-size:14px;line-height:1.6;color:#aab6cf;margin:8px 0;}'
      + '.code{font-family:Menlo,Consolas,monospace;font-size:15px;font-weight:700;letter-spacing:1px;'
      + 'display:inline-block;background:#0f172a;border:1px solid #334155;border-radius:8px;'
      + 'padding:12px 16px;margin:12px 0;color:#7dd3fc;}'
      + '.ok{color:#4ade80;font-weight:700;}'
      + '.warn{color:#fbbf24;}'
      + '.btn{display:inline-block;margin-top:14px;background:#2563eb;color:#fff;text-decoration:none;'
      + 'padding:11px 18px;border-radius:8px;font-weight:700;font-size:14px;}'

    if (!status) {
      return html('<html><head><meta charset="utf-8"><title>OmniTrade Hub</title><style>' + style
        + '</style></head><body><div class="box"><h1>OmniTrade Hub — Retour de paiement</h1>'
        + '<p>Vérification automatique en cours…</p></div></body></html>')
    }
    if (status !== 'successful') {
      return html('<html><head><meta charset="utf-8"><title>OmniTrade Hub</title><style>' + style
        + '</style></head><body><div class="box"><h1 class="warn">Paiement non confirmé</h1>'
        + '<p>Votre paiement n\u2019a pas été validé. Vous pouvez réessayer depuis la page d\u2019achat.</p></div></body></html>')
    }
    if (!flwSecret) {
      return html('<html><head><meta charset="utf-8"><title>OmniTrade Hub</title><style>' + style
        + '</style></head><body><div class="box"><h1 class="warn">Paiement reçu</h1>'
        + '<p>Votre paiement est bien arrivé, la remise du code se termine d\u2019ici quelques instants.</p>'
        + '<p>Si rien ne s\u2019affiche dans quelques minutes, contactez le vendeur en lui donnant la référence <b>'
        + (txRef || '—') + '</b>.</p></div></body></html>')
    }

    // On cherche un paiement existant par la référence, sinon par plan si vide.
    const { data: row } = txRef
      ? await supabase.from('paiements').select('*').eq('tx_ref', txRef).maybeSingle()
      : await (async () => ({ data: null }))()
    const effRef = row?.tx_ref || txRef
    const effPlan = row?.plan || plan
    const effEmail = row?.email || ''

    if (row?.code) {
      return html('<html><head><meta charset="utf-8"><title>OmniTrade Hub</title><style>' + style
        + '</style></head><body><div class="box"><h1 class="ok">Paiement confirmé ✅</h1>'
        + '<p>Voici votre code d\u2019achat :</p>'
        + '<span class="code">' + row.code + '</span>'
        + '<p>Ouvrez OmniTrade Hub sur votre ordinateur, page <b>Licence</b>, collez ce code et cliquez '
        + '« Activer automatiquement ».</p>'
        + '<a class="btn" href="https://github.com/MonsieurYannick/omnitrade-hub/releases">Télécharger l\u2019application</a>'
        + '</div></body></html>')
    }

    // Pas encore émis : on vérifie et on émet maintenant si possible.
    const code = await issueCode(effRef, effPlan, effEmail, txId)
    if (code) {
      return html('<html><head><meta charset="utf-8"><title>OmniTrade Hub</title><style>' + style
        + '</style></head><body><div class="box"><h1 class="ok">Paiement confirmé ✅</h1>'
        + '<p>Voici votre code d\u2019achat :</p>'
        + '<span class="code">' + code + '</span>'
        + '<p>Ouvrez OmniTrade Hub sur votre ordinateur, page <b>Licence</b>, collez ce code et cliquez '
        + '« Activer automatiquement ».</p>'
        + '<a class="btn" href="https://github.com/MonsieurYannick/omnitrade-hub/releases">Télécharger l\u2019application</a>'
        + '</div></body></html>')
    }

    return html('<html><head><meta charset="utf-8"><title>OmniTrade Hub</title><style>' + style
      + '</style></head><body><div class="box"><h1 class="warn">Paiement reçu, code en préparation</h1>'
      + '<p>Votre paiement est confirmé par le prestataire ; la remise du code arrive dans les toutes '
      + 'prochaines minutes (l\u2019activation se fait généralement en quelques secondes).</p>'
      + '<p>Référence : <b>' + (effRef || '—') + '</b></p>'
      + '<p>Si rien ne s\u2019affiche après quelques minutes, contactez le vendeur avec cette référence.</p>'
      + '</div></body></html>')
  }

  // ── webhook (POST) : Flutterwave notifie le paiement ─────────────────────
  // Signature : le header « verif-hash » doit valoir FLW_VERIF_HASH.
  const providedHash = (req.headers.get('verif-hash') || '').trim()
  if (!verifHash || providedHash !== verifHash) {
    return json(401, { ok: false, code: 'signature_invalide' })
  }
  try {
    const body = await req.json()
    const d = body?.data ?? body ?? {}
    const txRef = String(d.tx_ref || '').toUpperCase()
    const txId = String(d.id || d.transaction_id || '')
    const plan = String(d.meta?.plan || d.plan || '')
    const email = String(d.customer?.email || '')

    if (!txRef || !txId) return json(200, { ok: false, code: 'payload_incomplet' })

    const { data: row } = await supabase.from('paiements').select('*').eq('tx_ref', txRef).maybeSingle()
    if (!row || !plan || !PAYABLE[plan]) return json(200, { ok: false, code: 'introuvable' })

    if (row.code) return json(200, { ok: true, code: row.code }) // idempotent

    const v = await verifyTx(txId)
    if (!v || !v.ok || v.amount !== PAYABLE[plan] || v.cur !== DEVISE) {
      await supabase.from('paiements').update({ status: 'echec', tx_id: txId }).eq('tx_ref', txRef)
      return json(200, { ok: false, code: 'non_confirme' })
    }

    const code = await issueCode(txRef, plan, email || row.email, txId)
    return json(200, { ok: !!code, code: code ?? undefined })
  } catch {
    return json(200, { ok: false, code: 'technique' })
  }
}

Deno.serve(handler)