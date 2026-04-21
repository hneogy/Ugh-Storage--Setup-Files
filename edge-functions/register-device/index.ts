// Supabase edge function: register-device
//
// Validates a per-device provisioning token AND the user's Supabase JWT,
// provisions a Cloudflare tunnel, upserts a row in `devices`, and claims
// the provisioning token to the calling user. Transferable tokens: a user
// who already owns the token can re-register freely; anyone else gets a
// clear "already claimed" error.

import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'
import { serve } from 'https://deno.land/std@0.168.0/http/server.ts'

// Crockford-ish base32 (no I, L, O, U). Must stay in sync with iOS + bin/issue-tokens.py.
const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'

function isValidTokenFormat(raw: string): boolean {
  const compact = raw.trim().toUpperCase().replace(/-/g, '')
  if (!compact.startsWith('UGH')) return false
  if (compact.length !== 16) return false
  const body = compact.slice(3, 15)
  const check = compact[15]
  for (const c of body + check) {
    if (!ALPHABET.includes(c)) return false
  }
  let sum = 0
  for (const c of body) sum += ALPHABET.indexOf(c)
  return ALPHABET[sum % 32] === check
}

function canonicalToken(raw: string): string {
  const compact = raw.trim().toUpperCase().replace(/-/g, '')
  return `UGH-${compact.slice(3, 7)}-${compact.slice(7, 11)}-${compact.slice(11, 15)}-${compact.slice(15, 16)}`
}

serve(async (req) => {
  try {
    const authHeader = req.headers.get('Authorization')
    if (!authHeader) {
      return new Response(JSON.stringify({ error: 'Missing authorization' }), { status: 401 })
    }

    const supabaseUrl = Deno.env.get('SUPABASE_URL')!
    const supabaseServiceKey = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
    const supabase = createClient(supabaseUrl, supabaseServiceKey)

    const cfAccountId = Deno.env.get('CLOUDFLARE_ACCOUNT_ID')!
    const cfZoneId = Deno.env.get('CLOUDFLARE_ZONE_ID')!
    const cfApiToken = Deno.env.get('CLOUDFLARE_API_TOKEN')!

    const token = authHeader.replace('Bearer ', '')
    const { data: { user }, error: authError } = await supabase.auth.getUser(token)
    if (authError || !user) {
      return new Response(JSON.stringify({ error: 'Invalid token' }), { status: 401 })
    }

    const body = await req.json()
    const { hostname, storage_total, provisioning_token } = body

    // Validate provisioning token BEFORE any side effects so rejections
    // are cheap and don't leave orphan Cloudflare tunnels behind.
    if (!provisioning_token || typeof provisioning_token !== 'string') {
      return new Response(
        JSON.stringify({ error: 'provisioning_token is required', code: 'token_missing' }),
        { status: 400 }
      )
    }

    if (!isValidTokenFormat(provisioning_token)) {
      return new Response(
        JSON.stringify({ error: 'Invalid activation code format', code: 'token_malformed' }),
        { status: 400 }
      )
    }

    const canonical = canonicalToken(provisioning_token)
    const { data: tokenRow, error: tokenErr } = await supabase
      .from('provisioning_tokens').select('*').eq('token', canonical).single()

    if (tokenErr || !tokenRow) {
      return new Response(
        JSON.stringify({ error: "We don't recognize that activation code", code: 'token_not_found' }),
        { status: 404 }
      )
    }

    if (tokenRow.status === 'revoked') {
      return new Response(
        JSON.stringify({ error: 'This activation code has been revoked', code: 'token_revoked' }),
        { status: 403 }
      )
    }

    if (tokenRow.expires_at && new Date(tokenRow.expires_at) < new Date()) {
      return new Response(
        JSON.stringify({ error: 'This activation code has expired', code: 'token_expired' }),
        { status: 403 }
      )
    }

    // Transferable: re-register by the SAME user is fine; claimed by someone
    // else blocks until the other account unlinks.
    if (tokenRow.status === 'claimed' && tokenRow.claimed_by && tokenRow.claimed_by !== user.id) {
      return new Response(
        JSON.stringify({
          error: 'This activation code is already linked to another account. Ask the previous owner to unlink their device first.',
          code: 'token_claimed_by_other',
        }),
        { status: 409 }
      )
    }

    // From here matches the pre-token behavior: provision tunnel + DNS +
    // upsert device row + claim token at the end.
    let subdomain: string
    let attempts = 0
    while (true) {
      subdomain = Array.from(crypto.getRandomValues(new Uint8Array(4)))
        .map(b => b.toString(36).padStart(2, '0'))
        .join('')
        .slice(0, 8)
      const { data: existing } = await supabase
        .from('devices').select('id').eq('subdomain', subdomain).single()
      if (!existing) break
      if (++attempts > 10) throw new Error('Failed to generate unique subdomain')
    }

    const secretBytes = crypto.getRandomValues(new Uint8Array(32))
    const shared_secret = Array.from(secretBytes).map(b => b.toString(16).padStart(2, '0')).join('')

    const tunnelSecretBytes = crypto.getRandomValues(new Uint8Array(32))
    const tunnelSecret = btoa(String.fromCharCode(...tunnelSecretBytes))

    const tunnelName = `ugh-${subdomain}`
    const createTunnelRes = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${cfAccountId}/cfd_tunnel`,
      {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${cfApiToken}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: tunnelName, tunnel_secret: tunnelSecret }),
      }
    )
    const tunnelData = await createTunnelRes.json()
    if (!tunnelData.success) throw new Error(`Failed to create tunnel: ${JSON.stringify(tunnelData.errors)}`)

    const tunnelId = tunnelData.result.id
    const tunnelToken = tunnelData.result.token

    const configRes = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${cfAccountId}/cfd_tunnel/${tunnelId}/configurations`,
      {
        method: 'PUT',
        headers: { 'Authorization': `Bearer ${cfApiToken}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          config: {
            ingress: [
              { hostname: `${subdomain}.ughstorage.com`, service: 'http://localhost:8000' },
              { service: 'http_status:404' },
            ],
          },
        }),
      }
    )
    const configData = await configRes.json()
    if (!configData.success) {
      await fetch(`https://api.cloudflare.com/client/v4/accounts/${cfAccountId}/cfd_tunnel/${tunnelId}`, {
        method: 'DELETE', headers: { 'Authorization': `Bearer ${cfApiToken}` },
      })
      throw new Error(`Failed to configure tunnel: ${JSON.stringify(configData.errors)}`)
    }

    const dnsRes = await fetch(
      `https://api.cloudflare.com/client/v4/zones/${cfZoneId}/dns_records`,
      {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${cfApiToken}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: 'CNAME', name: `${subdomain}.ughstorage.com`,
          content: `${tunnelId}.cfargotunnel.com`, proxied: true,
        }),
      }
    )
    const dnsData = await dnsRes.json()
    if (!dnsData.success) console.warn('DNS creation warning:', JSON.stringify(dnsData.errors))

    const tunnel_url = `https://${subdomain}.ughstorage.com`

    const { data: existingDevice } = await supabase
      .from('devices').select('*').eq('owner_id', user.id).single()

    let device
    if (existingDevice) {
      if (existingDevice.tunnel_id && existingDevice.tunnel_id !== tunnelId) {
        try {
          await fetch(`https://api.cloudflare.com/client/v4/accounts/${cfAccountId}/cfd_tunnel/${existingDevice.tunnel_id}`,
            { method: 'DELETE', headers: { 'Authorization': `Bearer ${cfApiToken}` } })
        } catch (e) { console.warn('Failed to delete old tunnel:', e) }
      }
      const { data, error } = await supabase.from('devices').update({
        hostname, subdomain, shared_secret, tunnel_url, tunnel_id: tunnelId,
        tunnel_token: tunnelToken, storage_total: storage_total || 0,
        is_online: true, last_seen_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      }).eq('id', existingDevice.id).select().single()
      if (error) throw error
      device = data
    } else {
      const { data, error } = await supabase.from('devices').insert({
        owner_id: user.id, hostname, subdomain, shared_secret, tunnel_url,
        tunnel_id: tunnelId, tunnel_token: tunnelToken, storage_total: storage_total || 0,
        is_online: true, last_seen_at: new Date().toISOString(),
      }).select().single()
      if (error) throw error
      device = data
    }

    await supabase.from('provisioning_tokens').update({
      status: 'claimed',
      claimed_by: user.id,
      claimed_at: new Date().toISOString(),
      device_id: device.id,
    }).eq('token', canonical)

    return new Response(JSON.stringify({
      device_id: device.id, subdomain, tunnel_url, shared_secret, tunnel_token: tunnelToken,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  } catch (error) {
    return new Response(JSON.stringify({ error: error.message }), { status: 500 })
  }
})
