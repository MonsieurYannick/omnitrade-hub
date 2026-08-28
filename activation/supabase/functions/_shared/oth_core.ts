// oth_core.ts — Cœur de licence en TypeScript (Deno/Node), miroir EXACT de
// « 9-licence.py » de l'application OmniTradeHub.
//
// Garantie : une clé produite par `makeLicense` ici est vérifiable telle
// quelle par `check_license()` du moteur Python (Ed25519 RFC 8032 + base32
// RFC 4648 sans padding + JSON compact trié). NE PAS modifier les constantes
// ni l'ordre de sérialisation sous peine d'invalider toutes les licences.

const P = 2n ** 255n - 19n;
const L = 2n ** 252n + 27742317777372353535851937790883648493n;
const D = (-121665n * modpow(121666n, P - 2n, P)) % P;
const I = modpow(2n, (P - 1n) / 4n, P);

function modpow(base: bigint, exp: bigint, mod: bigint): bigint {
  base %= mod;
  if (base < 0n) base += mod;
  let res = 1n;
  while (exp > 0n) {
    if (exp & 1n) res = (res * base) % mod;
    base = (base * base) % mod;
    exp >>= 1n;
  }
  return res;
}

function modinv(x: bigint): bigint {
  const v = x % P;
  return modpow(v < 0n ? v + P : v, P - 2n, P);
}

function xRecover(y: bigint): bigint {
  let xx = ((y * y - 1n) * modinv(D * y * y + 1n)) % P;
  let x = modpow(xx, (P + 3n) / 8n, P);
  if ((x * x - xx) % P !== 0n) x = (x * I) % P;
  if (x % 2n !== 0n) x = P - x;
  return x;
}

const BY = (4n * modinv(5n)) % P;
const BX = xRecover(BY);
const B: Pt = [BX % P, BY % P, 1n, (BX * BY) % P];

type Pt = [bigint, bigint, bigint, bigint];

function ptAdd(p: Pt, q: Pt): Pt {
  const [x1, y1, z1, t1] = p;
  const [x2, y2, z2, t2] = q;
  const A = (y1 - x1) * (y2 - x2) % P;
  const B_ = (y1 + x1) * (y2 + x2) % P;
  const C = t1 * 2n * D * t2 % P;
  const D_ = z1 * 2n * z2 % P;
  const E = (B_ - A) % P;
  const F = (D_ - C) % P;
  const G = (D_ + C) % P;
  const H = (B_ + A) % P;
  const norm = (v: bigint) => ((v % P) + P) % P;
  return [norm(E * F), norm(G * H), norm(F * G), norm(E * H)];
}

function ptDbl(p: Pt): Pt {
  return ptAdd(p, p);
}

function ptMul(p: Pt, n: bigint): Pt {
  let q: Pt = [0n, 1n, 1n, 0n];
  while (n > 0n) {
    if (n & 1n) q = ptAdd(q, p);
    p = ptDbl(p);
    n >>= 1n;
  }
  return q;
}

function compress(p: Pt): Uint8Array {
  const [x1, y1, z1] = p;
  const zi = modinv(z1);
  const x = (x1 * zi) % P;
  const y = (y1 * zi) % P;
  return bigintToBytesLE(y | ((x & 1n) << 255n), 32);
}

function bytesToBigintLE(b: Uint8Array): bigint {
  let r = 0n;
  for (let i = b.length - 1; i >= 0; i--) r = (r << 8n) | BigInt(b[i]);
  return r;
}

function bigintToBytesLE(v: bigint, len: number): Uint8Array {
  const out = new Uint8Array(len);
  let x = v;
  for (let i = 0; i < len; i++) {
    out[i] = Number(x & 0xffn);
    x >>= 8n;
  }
  return out;
}

async function sha512(data: Uint8Array): Promise<Uint8Array> {
  const buf = await globalThis.crypto.subtle.digest("SHA-512", data as BufferSource);
  return new Uint8Array(buf);
}

async function secretExpand(sk: Uint8Array): Promise<[bigint, Uint8Array]> {
  const h = await sha512(sk);
  let a = bytesToBigintLE(h.slice(0, 32));
  a &= (1n << 254n) - 8n;
  a |= 1n << 254n;
  return [a, h.slice(32)];
}

export async function ed25519Sign(sk: Uint8Array, msg: Uint8Array): Promise<Uint8Array> {
  const [a, prefix] = await secretExpand(sk);
  const pk = compress(ptMul(B, a));
  const r = bytesToBigintLE(await sha512(concat(prefix, msg))) % L;
  const R = compress(ptMul(B, r));
  const k = bytesToBigintLE(await sha512(concat(R, pk, msg))) % L;
  const S = (r + k * a) % L;
  return concat(R, bigintToBytesLE(S, 32));
}

// ── base32 RFC 4648 sans padding (identique à « _b32e » du moteur) ──────
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

export function b32e(data: Uint8Array): string {
  let bits = "";
  for (const b of data) bits += b.toString(2).padStart(8, "0");
  let out = "";
  for (let i = 0; i < bits.length; i += 5) {
    const chunk = bits.slice(i, i + 5);
    out += B32[parseInt(chunk.padEnd(5, "0"), 2)];
  }
  return out;
}

export function normalizeMid(mid: string): string | null {
  if (!mid) return null;
  const v = mid.replace(/\s+/g, "").replace(/-/g, "").toUpperCase();
  return /^[A-Z2-7]{16}$/.test(v) ? v : null;
}

function group(s: string, n = 8): string {
  const parts: string[] = [];
  for (let i = 0; i < s.length; i += n) parts.push(s.slice(i, i + n));
  return parts.join("-");
}

function hexToBytes(hex: string): Uint8Array {
  const clean = hex.replace(/[^0-9a-fA-F]/g, "");
  const out = new Uint8Array(clean.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(clean.slice(i * 2, i * 2 + 2), 16);
  return out;
}

function randomBytes(n: number): Uint8Array {
  const b = new Uint8Array(n);
  globalThis.crypto.getRandomValues(b);
  return b;
}

function concat(...arrs: Uint8Array[]): Uint8Array {
  const total = arrs.reduce((s, a) => s + a.length, 0);
  const out = new Uint8Array(total);
  let o = 0;
  for (const a of arrs) {
    out.set(a, o);
    o += a.length;
  }
  return out;
}

function utcDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function addDays(dateStr: string, days: number): string {
  const [y, m, d] = dateStr.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCDate(dt.getUTCDate() + days);
  return utcDate(dt);
}

export interface LicensePayload {
  v: number;
  exp: string;
  mid: string;
  act: number;
  plan: string;
  iss: string;
  sn: string;
}

// Sérialisation DÉTERMINISTE : à l'identique de
// `json.dumps(payload, sort_keys=True, separators=(",", ":"))`.
// Valeurs toutes ASCII contrôlées : aucun échappement nécessaire.
function buildPayloadString(p: LicensePayload): string {
  return '{"act":' + p.act +
    ',"exp":"' + p.exp +
    '","iss":"' + p.iss +
    '","mid":"' + p.mid +
    '","plan":"' + p.plan +
    '","sn":"' + p.sn +
    '","v":' + p.v + '}';
}

function buildLicenseKey(raw: Uint8Array, sig: Uint8Array): string {
  return "OTH1-" + group(b32e(raw)) + "-" + group(b32e(sig));
}

export interface MakeLicenseResult {
  key: string;
  payload: LicensePayload;
}

// Plans identiques à « PLANS » de 9-licence.py (jours = durée d'une licence)
export const PLANS: Record<string, number | null> = {
  demo7: 7,
  m3: 90,
  m6: 180,
  m12: 365,
  life: null,
};

export async function makeLicense(
  skHex: string,
  plan: string,
  days: number | null,
  machineId: string,
  activations = 1,
  serial?: string,
): Promise<MakeLicenseResult> {
  const mid = normalizeMid(machineId);
  if (!mid) {
    throw new Error(
      "code machine obligatoire : 16 caracteres A-Z et 2-7.",
    );
  }
  machineId = mid;
  const now = utcDate(new Date());
  const exp = days ? addDays(now, days) : "never";
  const sn = serial || b32e(randomBytes(5));
  const payload: LicensePayload = { v: 1, exp, mid: machineId || "", act: activations, plan, iss: now, sn };
  const raw = new TextEncoder().encode(buildPayloadString(payload));
  const sig = await ed25519Sign(hexToBytes(skHex), raw);
  return { key: buildLicenseKey(raw, sig), payload };
}

export const _internals = { modinv, bytesToBigintLE, compress, b32e };