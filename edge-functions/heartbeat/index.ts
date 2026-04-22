// Heartbeat endpoint for the Pi → Supabase health channel.
//
// Why this exists: the `devices` table has an RLS policy
// `auth.uid() = owner_id` gating UPDATE. The Pi has no Supabase user
// session — it's a server-side actor — so a plain PostgREST PATCH with
// the anon key is always rejected by RLS. This function accepts a
// device-scoped credential (the `shared_secret` minted during
// `register-device`) and uses the service role internally to update
// the row after validating the caller owns that secret.
//
// Contract:
//   POST /functions/v1/heartbeat
//   Body: { device_id, shared_secret, storage_total?, storage_used?, firmware_version? }
//   200 OK  — row updated
//   400     — malformed body
//   401     — shared_secret mismatch (or device_id not found)
//   5xx     — internal error (also logged to function logs)
//
// verify_jwt is disabled (Pi has no Supabase session). The shared_secret
// check IS the auth.

import { createClient } from 'https://esm.sh/@supabase/supabase-js@2.45.4';

const SUPABASE_URL = Deno.env.get('SUPABASE_URL')!;
const SERVICE_ROLE_KEY = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!;

type HeartbeatBody = {
  device_id?: string;
  shared_secret?: string;
  storage_total?: number;
  storage_used?: number;
  firmware_version?: string;
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// Constant-time string compare so an attacker timing the response can't
// probe secret prefixes. Lengths differing short-circuits — that's
// acceptable here since the secret length is public knowledge anyway.
function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

Deno.serve(async (req: Request) => {
  if (req.method !== 'POST') {
    return jsonResponse(405, { error: 'method_not_allowed' });
  }

  let body: HeartbeatBody;
  try {
    body = await req.json();
  } catch {
    return jsonResponse(400, { error: 'invalid_json' });
  }

  const { device_id, shared_secret } = body;
  if (typeof device_id !== 'string' || typeof shared_secret !== 'string') {
    return jsonResponse(400, { error: 'missing_credentials' });
  }

  const admin = createClient(SUPABASE_URL, SERVICE_ROLE_KEY, {
    auth: { autoRefreshToken: false, persistSession: false },
  });

  // Fetch the row so we can compare the shared_secret. `.maybeSingle()`
  // returns null for unknown device_ids instead of throwing, which lets us
  // return a uniform 401 for both "wrong id" and "wrong secret" — that
  // way an attacker can't enumerate valid device ids.
  const { data: device, error: fetchErr } = await admin
    .from('devices')
    .select('id, shared_secret')
    .eq('id', device_id)
    .maybeSingle();

  if (fetchErr) {
    console.error('heartbeat: fetch failed', fetchErr);
    return jsonResponse(500, { error: 'fetch_failed' });
  }

  if (!device || !safeEqual(device.shared_secret, shared_secret)) {
    return jsonResponse(401, { error: 'unauthorized' });
  }

  // Build the patch. Only fields the caller sent are updated; the
  // heartbeat-universal ones (last_seen_at, is_online) always advance.
  const patch: Record<string, unknown> = {
    last_seen_at: new Date().toISOString(),
    is_online: true,
  };
  if (typeof body.storage_total === 'number') patch.storage_total = body.storage_total;
  if (typeof body.storage_used === 'number') patch.storage_used = body.storage_used;
  if (typeof body.firmware_version === 'string') patch.firmware_version = body.firmware_version;

  const { error: updateErr } = await admin
    .from('devices')
    .update(patch)
    .eq('id', device_id);

  if (updateErr) {
    console.error('heartbeat: update failed', updateErr);
    return jsonResponse(500, { error: 'update_failed' });
  }

  return jsonResponse(200, { ok: true, last_seen_at: patch.last_seen_at });
});
