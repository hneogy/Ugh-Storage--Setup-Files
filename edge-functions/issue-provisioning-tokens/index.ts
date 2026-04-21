// Supabase edge function: issue-provisioning-tokens
//
// Admin-only. Mints `count` new provisioning tokens, inserts them into
// the provisioning_tokens table, and returns the list. The CLI
// (bin/issue-tokens.py) calls this when packaging Pis.
//
// Auth model: caller MUST present a service-role JWT in the Authorization
// header. There's no per-user gate — only operators with the service-role
// key can call this. Keep that key offline; the CLI reads it from a local
// .env file or env var.

import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'
import { serve } from 'https://deno.land/std@0.168.0/http/server.ts'

const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'

function generateToken(): string {
  const buf = new Uint8Array(12)
  crypto.getRandomValues(buf)
  const body = Array.from(buf, b => ALPHABET[b % 32]).join('')
  let sum = 0
  for (const c of body) sum += ALPHABET.indexOf(c)
  const check = ALPHABET[sum % 32]
  return `UGH-${body.slice(0, 4)}-${body.slice(4, 8)}-${body.slice(8, 12)}-${check}`
}

serve(async (req) => {
  try {
    const authHeader = req.headers.get('Authorization')
    const expected = `Bearer ${Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')}`
    if (!authHeader || authHeader !== expected) {
      return new Response(JSON.stringify({ error: 'Service-role key required' }), { status: 401 })
    }

    const supabaseUrl = Deno.env.get('SUPABASE_URL')!
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
    const supabase = createClient(supabaseUrl, supabaseServiceKey)

    const body = await req.json().catch(() => ({}))
    const count = Math.max(1, Math.min(1000, body.count ?? 1))
    const sku = body.sku ?? null
    const batch_id = body.batch_id ?? null
    const expires_at = body.expires_at ?? null
    const notes = body.notes ?? null

    // Mint tokens, retrying on the (vanishingly unlikely) collision.
    const issued: string[] = []
    let collisionRetries = 0
    while (issued.length < count) {
      const token = generateToken()
      const { error } = await supabase.from('provisioning_tokens').insert({
        token, sku, batch_id, notes, expires_at,
      })
      if (error) {
        // Postgres unique-violation code is 23505. Anything else is unexpected.
        if (error.code === '23505') {
          if (++collisionRetries > 100) throw new Error('Too many token collisions; PRNG broken?')
          continue
        }
        throw error
      }
      issued.push(token)
    }

    return new Response(JSON.stringify({
      count: issued.length,
      tokens: issued,
      sku, batch_id, expires_at,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), { status: 500 })
  }
})
