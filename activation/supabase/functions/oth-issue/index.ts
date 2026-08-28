// oth-issue — OUTIL VENDEUR : crée, liste, révoque et consulte les codes
// d'achat. Protégé par un mot de passe administrateur (header « x-oth-admin »).
// C'est aussi par-là que passera le paiement Stripe (webhook, plus tard).
//
// DÉPLOIEMENT : supabase functions deploy oth-issue --no-verify-jwt
// SECRETS :     OTH_ADMIN_KEY → mot de passe vendeur (long, aléatoire)
//
// Appel :
//   { action:'create', plan:'m12', days?, max_activations?, expires_at?, customer?, note? }
//   { action:'list',   plan? }
//   { action:'get',    code }
//   { action:'revoke', code, revoked?:true|false }
//   { action:'stats' }
import { createClient, SupabaseClient } from 'https://esm.sh/@supabase/supabase-js@2'

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type, x-oth-admin',
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...cors },
  })
}

const PLANS = new Set(['demo7', 'm3', 'm6', 'm12', 'life'])
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

function newCode(): string {
  const t = randB32(10) // 10 octets -> 16 caractères base32 exactement
  return 'OTH-' + t.slice(0, 8) + '-' + t.slice(8)
}

export async function handler(req: Request, ctx?: { env?: Record<string, string> }): Promise<Response> {
  if (req.method === 'OPTIONS') return new Response('ok', { status: 204, headers: cors })

  const env = ctx?.env ?? (Deno as any).env.toObject?.() ?? {}
  const supabaseUrl = env.SUPABASE_URL || ''
  const serviceKey = env.SUPABASE_SERVICE_ROLE_KEY || ''
  const adminKey = env.OTH_ADMIN_KEY || ''
  if (!supabaseUrl || !serviceKey || !adminKey) {
    return json(500, { ok: false, error: 'configuration', msg: 'Secrets manquants.' })
  }

  // Haute sécurité : le mot de passe vendeur DOIT matcher, sauf pour les
  // webhooks Stripe qui, eux, seront validés par la signature (future PR).
  const provided = (req.headers.get('x-oth-admin') || '').trim()
  if (provided !== adminKey) return json(401, { ok: false, error: 'non_autorise' })

  let body: any
  try {
    body = await req.json()
  } catch {
    return json(400, { ok: false, error: 'json', msg: 'Requête invalide.' })
  }

  const supabase: SupabaseClient = createClient(supabaseUrl, serviceKey)
  const action = body.action || ''

  try {
    switch (action) {
      case 'create': {
        const plan = (body.plan || 'm12').toLowerCase()
        if (!PLANS.has(plan)) return json(400, { ok: false, error: 'plan_inconnu' })
        let days = body.days
        if (days === undefined || days === null || days === '') days = null
        else days = Number(days)
        if (days !== null && (!Number.isFinite(days) || days < 1)) {
          return json(400, { ok: false, error: 'jours_invalides' })
        }
        const maxAct = body.max_activations === undefined || body.max_activations === null
          ? 2
          : Number(body.max_activations)
        const code = newCode()
        const rec = {
          code,
          plan,
          days,
          max_activations: maxAct,
          customer: body.customer ?? null,
          note: body.note ?? null,
          expires_at: body.expires_at && !Number.isNaN(Date.parse(body.expires_at))
            ? new Date(body.expires_at).toISOString()
            : null,
        }
        const { error } = await supabase.from('purchase_codes').insert(rec)
        if (error) return json(500, { ok: false, error: 'insert', msg: error.message })
        return json(200, { ok: true, code, plan, days, max_activations: maxAct })
      }

      case 'list': {
        let q = supabase
          .from('purchase_codes')
          .select('code, plan, days, max_activations, machines, created_at, expires_at, revoked, customer, note')
          .order('created_at', { ascending: false })
        if (body.plan) q = q.eq('plan', (body.plan as string).toLowerCase())
        if (body.revoked === true) q = q.eq('revoked', true)
        if (body.revoked === false) q = q.eq('revoked', false)
        const { data, error } = await q.limit(body.limit ? Math.min(Number(body.limit), 500) : 100)
        if (error) return json(500, { ok: false, error: 'select', msg: error.message })
        const rows = (data ?? []).map((r: any) => ({
          code: r.code,
          plan: r.plan,
          days: r.days,
          revoke: r.revoked,
          activations: Array.isArray(r.machines) ? r.machines.length : 0,
          max_activations: r.max_activations,
          expires_at: r.expires_at,
          created_at: r.created_at,
          customer: r.customer,
          note: r.note,
        }))
        return json(200, { ok: true, rows })
      }

      case 'get': {
        const code = (body.code || '').toUpperCase()
        const { data, error } = await supabase
          .from('purchase_codes')
          .select('*')
          .eq('code', code)
          .maybeSingle()
        if (error) return json(500, { ok: false, error: 'select', msg: error.message })
        if (!data) return json(404, { ok: false, error: 'introuvable', msg: `Code ${code} introuvable.` })
        return json(200, { ok: true, ...data })
      }

      case 'revoke': {
        const code = (body.code || '').toUpperCase()
        const rep = body.revoked === false ? false : true
        const { error } = await supabase
          .from('purchase_codes')
          .update({ revoked: rep })
          .eq('code', code)
        if (error) return json(500, { ok: false, error: 'update', msg: error.message })
        return json(200, { ok: true, code, revoked: rep })
      }

      case 'stats': {
        const { count, error } = await supabase
          .from('purchase_codes')
          .select('code', { count: 'exact', head: true })
        if (error) return json(500, { ok: false, error: 'select', msg: error.message })
        return json(200, { ok: true, total_codes: count })
      }

      default:
        return json(400, {
          ok: false,
          error: 'action_inconnue',
          actions: ['create', 'list', 'get', 'revoke', 'stats'],
        })
    }
  } catch (e: any) {
    return json(500, { ok: false, error: 'technique', msg: String(e?.message ?? e) })
  }
}

Deno.serve(handler)