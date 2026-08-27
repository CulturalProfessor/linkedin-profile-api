# LinkedIn Profile API

Public HTTPS API: LinkedIn profile URL in, structured JSON out (name, headline,
location, about, experience, education, skills, certifications, languages,
images). A purely reverse-engineered solution that calls LinkedIn's own
internal endpoints directly - **no browser automation, no HTML/JSON-LD
scraping.**

```
GET /profile?url=https://www.linkedin.com/in/someone
x-li-at: <your li_at cookie>
x-jsessionid: <your JSESSIONID cookie>
```

Interactive docs at `/docs` (FastAPI's auto-generated OpenAPI UI).

## Approach

LinkedIn's web profile page is now server-driven UI backed by
`/voyager/api/graphql`. The old one-call aggregators
(`identity/profileView`, `identity/dash/profileCards`,
`identity/dash/profileComponents`) are retired and 404. What's still live is
a set of **per-section dash endpoints** - the same architecture the profile
page's own JS uses:

1. **Resolve once**: `GET /voyager/api/identity/dash/profiles?q=memberIdentity&memberIdentity={publicId}`
   returns the profile entity and its URN (`urn:li:fsd_profile:...`).
2. **Fan out**: for each section (`profilePositionGroups`, `profileEducations`,
   `profileSkills`, `profileCertifications`, `profileLanguages`, and a few
   optional ones), `GET /voyager/api/identity/dash/{section}?q=viewee&profileUrn={urn}`.
3. **Denormalize**: every response has the same shape -
   `{"data": {"*elements": [urn, ...]}, "included": [entity, ...]}`.
   `included` is an unordered bag; `app/denormalize.py` builds an
   `entityUrn -> entity` index and walks `*elements` in order to reassemble
   each section, then maps LinkedIn's internal field names onto the API's
   output shape (see [`app/models.py`](app/models.py)).

The response shapes were captured live against a real profile during
development, then rebuilt as two fully synthetic fixtures for the repo -
`fixtures/sample_raw.json` and `fixtures/sample_raw_notable.json` - so no
real person's data ships in a public repo. Both are consumed field-for-field
by [`tests/test_denormalize.py`](tests/test_denormalize.py); together they
cover in-progress roles (no end date), title-joining via `profilePositions`,
multiple degrees, and entirely absent optional sections. Step 1 (resolving a
public identifier to its URN via `profiles?q=memberIdentity`) was separately
confirmed live, returning a 200 with the expected profile entity and
headline.

### Why not the official API?

LinkedIn's OAuth API only returns the *authenticated user's own* profile -
there's no arbitrary-profile-by-URL endpoint on it. It's a dead end for this
task by design, not an oversight.

## Auth model

The caller supplies **their own** LinkedIn session via two headers:

```
x-li-at: <li_at cookie value>
x-jsessionid: <JSESSIONID cookie value>
```

Both come from a normal logged-in browser session (DevTools → Application →
Cookies). They're used in-memory for that one request and never stored or
logged. If neither header is sent, the backend falls back to an optional demo
session configured via `LINKEDIN_LI_AT` / `LINKEDIN_JSESSIONID` environment
variables - never committed to the repo.

**Getting your own session values**: paste
[`tools/get_session_cookie.js`](tools/get_session_cookie.js) into your
browser's DevTools console while logged into linkedin.com. It reads your
`JSESSIONID` cookie automatically and walks you through copying `li_at`
manually - `li_at` is `HttpOnly`, so no page script (this one included) is
allowed to read it; that's the browser protecting you from exactly this kind
of script being able to steal it via XSS, not a gap in the snippet. Nothing
it does leaves your own browser - no network calls, no data sent anywhere.

This is deliberately **not** a username/password login form. That shape
looks like phishing, and it breaks on 2FA. Cookie-based auth against a
caller-held session is the same model PhantomBuster (cookie via browser
extension) and Unipile (cookie-based connect) use in production.

### Legal / account-safety framing

- Scraping publicly visible data isn't a CFAA violation (*hiQ Labs v.
  LinkedIn*, 9th Cir.) - the real exposure is LinkedIn's Terms of Service
  (breach of contract), not criminal liability.
- Both LinkedIn ToS lawsuits that actually landed hard (hiQ's underlying
  conduct, and Proxycurl/Nubela in 2025) involved scraping at high volume
  through throwaway/bulk accounts. Low-volume reads through one real,
  established account sit at the bottom of that risk ladder.
- PhantomBuster's own published safe limit is ~1,500 profile views/day/account.
  This project's built-in `DAILY_QUOTA` defaults to 50, and realistic use
  (demoing this API) touches on the order of 10 profiles.
- Guardrails in `app/rate_limit.py` and `app/config.py`: a jittered delay
  between live requests (avoids an even-interval timing signature), a hard
  daily quota **per LinkedIn account**, and a kill switch
  (`ALLOW_LIVE=false`) that stops all live traffic and serves cache-only.

None of this makes scraping risk-free - it's a judgment call about where on
the risk spectrum this sits, not a legal opinion.

### Quota is per-account, not global

The quota exists to cap exposure on *one specific LinkedIn account* - so it's
keyed by account, not shared across every request the service handles. Each
request derives an `account_key` (`app/main.py::_account_key`, a truncated
SHA-256 hash of whichever `li_at` ends up in use - the raw cookie is never
used as a key or written anywhere) and the quota is tracked per key:

- The **backend demo session** (if configured via `LINKEDIN_LI_AT`) has its
  own bucket, capped at `DAILY_QUOTA`.
- Each **caller-supplied session** (`x-li-at` header) gets its own separate
  bucket, keyed off their own cookie's hash.

This matters because a caller bringing their own session is spending *their*
account's risk budget, not the demo account's - a single global counter
would let one caller's traffic exhaust the demo session's daily quota (or
vice versa), and would meaninglessly conflate the risk exposure of several
unrelated real accounts under one number. `/health` reports
`backend_session_remaining_quota_today` for the configured demo session
specifically; a caller's own bucket isn't exposed there since it's scoped to
their own session, not the deployment operator's.

### Shared quota across local + deployed runs

Within one account's bucket, the count also needs to be tracked globally
across *processes*, not just requests - running this locally while it's also
deployed shouldn't let two independent counters each think the same account
has a fresh `DAILY_QUOTA`. `app/quota.py` defines a pluggable `QuotaBackend`,
keyed by `account_key`:

- **`InMemoryQuotaBackend`** (default when unconfigured) - counts in that
  one process only. Fine for solo local testing, but a laptop run and a
  deployed instance won't see each other's usage.
- **`UpstashQuotaBackend`** - a shared counter via
  [Upstash](https://upstash.com)'s free Redis REST API. Both environments
  hit the same HTTPS endpoint, so the count is genuinely global per account.
  Set `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` (from an
  Upstash Redis database's dashboard) to enable it - `/health` reports
  `shared_quota_store: true` once it's active.

## Running locally

```bash
python3 -m pip install --user -r requirements-dev.txt
cp .env.example .env   # fill in a session if you want live fetches
python3 -m uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000/docs`.

## Tests

```bash
python3 -m pytest
```

`tests/test_denormalize.py` runs the denormalizer against the two synthetic
fixtures with no network access required.

## Deployment

Any host that runs a standard ASGI app works (Railway, Render, Fly.io).
Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set environment variables from `.env.example` in the host's dashboard - never
commit real `LINKEDIN_LI_AT` / `LINKEDIN_JSESSIONID` values. Also set
`UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` here and in your local
`.env` if you want local runs and the deployed server to share one daily
quota (see [Shared quota](#shared-quota-across-local--deployed-runs) above).

## API

### `GET /profile`

| Param / Header | Required | Description |
|---|---|---|
| `url` (query) | yes | Full profile URL or bare public identifier |
| `force_refresh` (query) | no | Bypass cache and re-fetch live |
| `x-li-at` (header) | no* | Caller's `li_at` cookie |
| `x-jsessionid` (header) | no* | Caller's `JSESSIONID` cookie |

\* required unless a backend demo session is configured.

Responses: `200` with the profile JSON, `401` (no usable session / session
rejected by LinkedIn), `404` (profile not found), `429` (daily quota
exhausted), `503` (kill switch engaged).

### `GET /health`

Liveness + remaining daily quota, no auth required.

## Limitations

- **No city-level location.** LinkedIn's profile entity only returns a
  `countryCode` now; the human-readable city lives behind
  `geoLocation.geoUrn`, which needs a separate, undocumented geo-resolve call
  this project doesn't make yet.
- **Job titles depend on an unverified endpoint.** Company name and dates
  come from `profilePositionGroups`; titles are joined in from a separate
  `profilePositions` endpoint (`app/voyager_client.py`,
  `app/denormalize.py`). The join logic is exercised in
  `fixtures/sample_raw.json`, but the endpoint itself hasn't been confirmed
  against live LinkedIn traffic yet - if it 404s, experience entries just
  fall back to no title (see the other synthetic fixture for that path).
- **These are internal, undocumented endpoints.** LinkedIn can change field
  names, response shapes, or retire endpoints without notice - as it already
  did to the old aggregator endpoints this project had to route around.
- **Fixtures are synthetic, not real scraped data.** No real profile's
  content ships in this repo; `fixtures/sample_raw.json` and
  `sample_raw_notable.json` are hand-built in the real Voyager response
  shape to exercise the denormalizer, not actual LinkedIn responses.
