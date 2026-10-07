// Shared helpers. Every Jibsy call goes through api(), with a path relative to
// /api/v1/commai/customers/{customer_id}. The business is the one the API key
// belongs to (GET /api/v1/auth/me), so a person never types an id.
const crypto = require('crypto');

const base = (bundle) => String(bundle.authData.base_url || '').replace(/\/+$/, '');

const me = async (z, bundle) => {
  const r = await z.request({ method: 'GET', url: `${base(bundle)}/api/v1/auth/me` });
  r.throwForStatus();
  return r.data;
};

const api = async (z, bundle, method, path, body, params) => {
  const who = await me(z, bundle);
  if (!who.customer_id) throw new z.errors.Error('This API key is not linked to a business.', 'NoBusiness', 403);
  const r = await z.request({
    method,
    url: `${base(bundle)}/api/v1/commai/customers/${who.customer_id}${path}`,
    body,
    params,
    skipThrowForStatus: true,
  });
  if (r.status === 429) throw new z.errors.ThrottledError('Jibsy asked us to slow down.', 30);
  if (r.status >= 400) {
    const detail = (r.data && r.data.detail) || r.content;
    const text = typeof detail === 'string' ? detail : JSON.stringify(detail);
    throw new z.errors.Error(`Jibsy said: ${text}`, 'ApiError', r.status);
  }
  return r.status === 204 ? {} : r.data;
};

// X-ExaCarib-Signature: v1=<hex HMAC-SHA256 of "<timestamp>.<body>">, five-minute window.
const verify = (secret, headers, body) => {
  const h = {};
  Object.keys(headers || {}).forEach((k) => {
    h[k.toLowerCase()] = headers[k];
  });
  const ts = Number(h['x-exacarib-timestamp'] || 0);
  const sig = String(h['x-exacarib-signature'] || '');
  if (!secret || !ts || Math.abs(Date.now() / 1000 - ts) > 300) return false;
  const want = 'v1=' + crypto.createHmac('sha256', secret).update(`${ts}.${body}`).digest('hex');
  return sig.length === want.length && crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(want));
};

module.exports = { api, base, me, verify };
