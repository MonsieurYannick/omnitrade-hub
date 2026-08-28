// test_sign.mjs — signe une licence avec oth_core.ts puis la vérifie via le
// moteur Python 9-licence.py. Confirme une compatibilité clé-pour-clé.
import { makeLicense } from "../supabase/functions/_shared/oth_core.ts";

const SK = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const MID = "MBK7UVEURZDSD35Z";

const cases = [
  { plan: "demo7", days: 7, activations: 1 },
  { plan: "m12", days: 365, activations: 1 },
  { plan: "life", days: null, activations: 1 },
  { plan: "m6", days: 180, activations: 2 },
];

for (const c of cases) {
  const { key, payload } = await makeLicense(SK, c.plan, c.days, MID, c.activations);
  console.log(JSON.stringify({ plan: c.plan, key, payload, mid: MID }));
}