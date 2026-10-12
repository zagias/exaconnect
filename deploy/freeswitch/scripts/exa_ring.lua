-- Ring a closed Jibsy Phone (ADR 0045). Run by each extension's dial plan just
-- before its phones ring: exa_ring.lua <tenant> <extension> <domain> <controller url>
--
-- If the extension's app is connected (Verto), nothing to do. Otherwise ask the
-- controller to wake the person's devices with a push message (signed like the
-- outside-call check: md5(secret:tenant:ext::caller:call:secret), see voice/pbx.py)
-- and, if it reached any, keep the caller hearing ringing while the app opens and
-- connects, for up to WAIT seconds. Then the dial plan rings the extension as usual.
local WAIT = 25

local tenant, ext, dom, url = argv[1], argv[2], argv[3], argv[4]
if not (session and session:ready() and tenant and ext and dom and url) then return end
local api = freeswitch.API()

local function app_connected()
  local c = api:execute("verto_contact", ext .. "@" .. dom) or ""
  return c ~= "" and not c:find("error/", 1, true)
end

if app_connected() then return end
local secret = freeswitch.getGlobalVariable("exa_pbx_secret") or ""
if secret == "" then return end

local caller = (session:getVariable("caller_id_number") or ""):gsub("[^%d+]", ""):sub(1, 16)
local name = (session:getVariable("caller_id_name") or ""):gsub("[^%w %.%-']", ""):sub(1, 60)
local call = session:get_uuid()
local digest = api:execute("md5", table.concat({ secret, tenant, ext, "", caller, call, secret }, ":"))
local function q(s) return (s:gsub("[^%w%-%._~]", function(c) return string.format("%%%02X", string.byte(c)) end)) end
local ask = string.format("%s/api/v1/commai/internal/voice/ring?tenant=%s&ext=%s&caller=%s&name=%s&call=%s",
  url, q(tenant), q(ext), q(caller), q(name), q(call))
local answer = api:execute("curl", ask .. " connect-timeout 2 timeout 8 append_headers X-Exa-Pbx-Auth:" .. digest .. " get") or ""
if not answer:find("^PUSHED") then return end

freeswitch.consoleLog("info", "exacarib: woke the app for " .. ext .. " (" .. answer .. ")\n")
session:execute("ring_ready")
for _ = 1, WAIT * 2 do
  if not session:ready() then return end
  if app_connected() then
    session:sleep(800) -- the app has signed in; let it settle before the call reaches it
    freeswitch.consoleLog("info", "exacarib: app for " .. ext .. " connected, ringing it\n")
    return
  end
  session:sleep(500)
end
