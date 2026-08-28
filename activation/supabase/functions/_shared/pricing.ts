// pricing.ts — Plans vendus par le site de paiement (oth-purchase).
//
// ⚠️ TARIFS À AJUSTER LIBREMENT (prix affichés et facturés, en FCFA).
// Le webhook vérifie que le montant payé correspond au prix du plan :
// changer un prix ici change automatiquement le montant attendu.

// Plans payables (code de la licence = même plan que oth-issue).
export const PAYABLE: Record<string, number> = {
  m3: 25000, // 3 mois
  m6: 40000, // 6 mois
  m12: 65000, // 12 mois
  life: 150000, // à vie
}

// Durée (jours) de licence accordée par plan — miroir de oth_core.ts PLANS.
export const DAYS: Record<string, number | null> = {
  demo7: 7,
  m3: 90,
  m6: 180,
  m12: 365,
  life: null,
}

export const DEVISE = 'XOF'

export function planLabel(plan: string): string {
  const m: Record<string, string> = {
    demo7: 'Essai 7 jours',
    m3: '3 mois',
    m6: '6 mois',
    m12: '12 mois',
    life: 'Licence à vie',
  }
  return m[plan] ?? plan
}