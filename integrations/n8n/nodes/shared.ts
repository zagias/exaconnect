// Shared helpers. Every Jibsy call goes through commAiRequest(), with a path
// relative to /api/v1/commai/customers/{customer_id}. The business is the one
// the API key belongs to (GET /api/v1/auth/me).
import { createHmac, timingSafeEqual } from 'crypto';
import type { IDataObject, IExecuteFunctions, IHookFunctions, ILoadOptionsFunctions, IHttpRequestMethods } from 'n8n-workflow';
import { NodeApiError } from 'n8n-workflow';

type Ctx = IExecuteFunctions | IHookFunctions | ILoadOptionsFunctions;

async function base(ctx: Ctx): Promise<string> {
  const creds = await ctx.getCredentials('commAiApi');
  return String(creds.baseUrl || '').replace(/\/+$/, '');
}

export async function me(ctx: Ctx): Promise<IDataObject> {
  return (await ctx.helpers.httpRequestWithAuthentication.call(ctx, 'commAiApi', {
    method: 'GET',
    url: `${await base(ctx)}/api/v1/auth/me`,
    json: true,
  })) as IDataObject;
}

export async function commAiRequest(
  ctx: Ctx,
  method: IHttpRequestMethods,
  path: string,
  body?: IDataObject,
  qs?: IDataObject,
): Promise<any> {
  const who = await me(ctx);
  if (!who.customer_id) throw new Error('This API key is not linked to a business.');
  try {
    return await ctx.helpers.httpRequestWithAuthentication.call(ctx, 'commAiApi', {
      method,
      url: `${await base(ctx)}/api/v1/commai/customers/${who.customer_id}${path}`,
      body,
      qs,
      json: true,
    });
  } catch (error) {
    throw new NodeApiError(ctx.getNode(), error as any);
  }
}

// X-ExaCarib-Signature: v1=<hex HMAC-SHA256 of "<timestamp>.<body>">, five-minute window.
export function verify(secret: string, headers: IDataObject, body: string): boolean {
  const ts = Number(headers['x-exacarib-timestamp'] || 0);
  const sig = String(headers['x-exacarib-signature'] || '');
  if (!secret || !ts || Math.abs(Date.now() / 1000 - ts) > 300) return false;
  const want = 'v1=' + createHmac('sha256', secret).update(`${ts}.${body}`).digest('hex');
  return sig.length === want.length && timingSafeEqual(Buffer.from(sig), Buffer.from(want));
}
