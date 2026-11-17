# Edge-TTS MCP: Railway + ClickUp

This fork adds a secured Streamable HTTP MCP server without changing the upstream
library. Upstream license files are unchanged; added wrapper code is
LGPL-3.0-or-later. This is not an official Microsoft service.

## Cost and privacy

No paid speech API key is currently required. There is no unlimited-use or
continued-availability guarantee. Microsoft can throttle or change the service;
review applicable terms before production/commercial use. Railway usage is billed
under your existing plan. Text is sent to Microsoft. Do not submit confidential
student information or private records without appropriate authorization.

## Railway deployment

1. Create a service from this GitHub fork, branch `master`.
2. Allow Railway access to the repository if requested. The root Dockerfile and
   railway.toml supply build, start, and health-check configuration.
3. Set `MCP_API_KEY` to a random secret of at least 32 characters. Generate locally:
   `openssl rand -hex 32` or
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
   Keep it in Railway Variables, never GitHub, task descriptions, or chat messages.
4. Set `PORT=8000`. Enable public networking, generate an HTTPS domain, and target
   port 8000. This avoids ambiguity about the Railway-generated PORT value.
5. Set `PUBLIC_BASE_URL` to that HTTPS origin, with no path. Alternatively the app
   derives it from `RAILWAY_PUBLIC_DOMAIN` when Railway provides that variable.
6. Redeploy and check `/health`. Health verifies the wrapper, not Microsoft.
7. Use ONE replica. Audio is ephemeral local storage. Multiple replicas can route
   downloads to the wrong instance. Redeploys/restarts remove existing audio.

Optional: `AUDIO_TTL_SECONDS=3600` (range 60..86400),
`AUDIO_DIR=/tmp/edge-tts-audio`. No persistent volume is required.

## Connect to GrowMore Company in ClickUp

A workspace owner/admin completes this in the ClickUp browser app:

1. App Center > Custom MCP > Add New.
2. Choose **For all members / Workspace**.
3. Name: `Edge TTS`.
4. URL: your public Railway HTTPS origin followed by `/mcp`.
5. Authentication: API key. The outgoing header must be
   `Authorization: Bearer <MCP_API_KEY>`. If configuring a custom header, use
   `Authorization` as the header and `Bearer ` plus the secret as its value.
   Avoid a duplicated Bearer prefix if the UI adds it automatically. Never put the
   secret in the URL. Do not choose unauthenticated access.
6. Connect. Confirm `list_voices` and `generate_speech` are listed.

No ClickUp API token is needed by Railway. This service does not read tasks or
post attachments. Workspace-wide access lets members consume Railway resources.

## Tools

- `list_voices(language="all")`: accepts all, ar, en, ar-SA, ar-EG, en-US, etc.
- `generate_speech(text, voice="auto", rate="+0%", pitch="+0Hz", volume="+0%")`:
  returns MP3 download URL, byte size, voice, and expiration.
- Auto voice: Saudi Arabic female for Arabic text, US English female otherwise.
  Explicitly select a name from list_voices for other dialects/genders. Generate
  English and Arabic versions separately for predictable pronunciation.

Limits: 20,000 characters/call, two concurrent generations, 180 seconds/generation,
32 MB/file, about 256 MB temporary storage. These are wrapper safeguards, not a
Microsoft allowance. Voice lists are cached for six hours.

Signed download links expire after one hour by default. Anyone with the link can
download until expiry. Save audio promptly. Rotating MCP_API_KEY invalidates links.
Expired files are cleaned every minute; expiry is checked on every download.
Access logs are disabled to avoid logging signed links. Railway/proxy logging has
its own policies. There is no permanent hosting or automatic ClickUp attachment.

## Test and verify

With Python 3.12, from the repository root:

```sh
pip install . -r requirements-mcp.txt httpx
python -m unittest discover -s tests -p 'test_railway_mcp.py' -v
```

Tests mock Microsoft and cover auth, MCP initialization/discovery/invocation,
downloads, tampering, expiry, controls, and cleanup. They do not prove live upstream
availability. The preparation environment only ran syntax/TOML and standalone
control-validation/signing checks. Full integration tests have NOT been run there
because it has no internet access and lacks the server dependencies. GitHub Actions
was not enabled: the connected GitHub account integration lacks permission to write
workflow files. Run the commands above before production use.

After deployment, set BASE_URL to the Railway HTTPS origin and MCP_API_KEY in your
local shell, then run:

```sh
curl --fail "$BASE_URL/health"
curl --fail -X POST "$BASE_URL/mcp" \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}'
curl --fail -X POST "$BASE_URL/mcp" \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

Finally generate a short Arabic recording and a short English recording from
ClickUp. Open both download links. Only then is end-to-end operation verified.

## Troubleshooting

- Startup error: verify key length and public origin variables.
- 401: missing/wrong Authorization or duplicated Bearer prefix.
- Invalid Host: PUBLIC_BASE_URL differs from the domain you used.
- Connection failure: use `/mcp`, not `/health`, `/sse`, or the repository URL.
- Speech failure while health is OK: retry later, shorter text; Microsoft can
  throttle or be unavailable. No automatic paid fallback is configured.
- Download 403/404: expired link, rotated key, removed file, or restarted service.
