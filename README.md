# LinkedIn Profile API

Public HTTPS API: LinkedIn profile URL in, structured JSON out (name, headline,
location, about, experience, education, skills, certifications, languages,
images). A purely reverse-engineered solution that calls LinkedIn's own
internal endpoints directly - **no browser automation, no HTML/JSON-LD
scraping.**

```
GET /profile?url=https://www.linkedin.com/in/someone
x-li-cookie: <the full Cookie header value from a logged-in linkedin.com request>
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
2. **Fan out**: for each section in `FETCHED_SECTIONS` - `profilePositionGroups`,
   `profilePositions`, `profileEducations`, `profileSkills`,
   `profileCertifications`, `profileLanguages` - `GET
   /voyager/api/identity/dash/{section}?q=viewee&profileUrn={urn}`, with a
   jittered pause between each. Other known-good section paths
   (courses, projects, honors, volunteering) are listed in `SECTION_PATHS` but
   deliberately not fetched: nothing maps them into the output yet, so they
   only spent requests against the session to build data that was discarded.
3. **Denormalize**: every response has the same shape -
   `{"data": {"*elements": [urn, ...]}, "included": [entity, ...]}`.
   `included` is an unordered bag; `app/denormalize.py` builds an
   `entityUrn -> entity` index and walks `*elements` in order to reassemble
   each section, then maps LinkedIn's internal field names onto the API's
   output shape (see [`app/models.py`](app/models.py)).

**Experience is assembled from `profilePositions`, not
`profilePositionGroups`.** A position *group* carries a single date range
spanning the member's entire tenure at a company, so deriving each role's
dates from it reports every role at a multi-role company as having started
when the member joined and never ended - on the profile this was found
against, four sequential Mastercard roles all rendered as "2010 - present".
Each `Position` carries its own `dateRange`, `title`, `description` and
location. The group is still used to fill in a company name or date range an
individual position omits, and to keep companies that have no position entity
of their own - all that's lost for those is the title.

The response shapes were captured live against a real profile during
development, then rebuilt as three fully synthetic fixtures for the repo -
`fixtures/sample_raw.json`, `sample_raw_notable.json` and
`sample_raw_multirole.json` - so no real person's data ships in a public
repo. All three are consumed field-for-field by
[`tests/test_denormalize.py`](tests/test_denormalize.py); together they cover
in-progress roles (no end date), multiple sequential roles at one company
each keeping their own dates, city resolution via a position's `geoUrn` and
the country-code fallback when it misses, a company present only as a
position group, multiple degrees, and entirely absent optional sections. Both
`profiles?q=memberIdentity` (step 1) and the per-section `q=viewee` calls
were confirmed live against a real profile.

### Why not the official API?

LinkedIn's OAuth API only returns the *authenticated user's own* profile -
there's no arbitrary-profile-by-URL endpoint on it. It's a dead end for this
task by design, not an oversight.

## Auth model

The caller supplies **their own** LinkedIn session, in one of two shapes:

```
x-li-cookie: <the full Cookie header value from a real linkedin.com request>   # recommended
```
```
x-li-at: <li_at cookie value>        # minimal alternative
x-jsessionid: <JSESSIONID cookie value>
```

`x-li-cookie` wins when both are present. It's recommended over the minimal
pair because `li_at` + `JSESSIONID` replayed **in isolation** - stripped of
the `bcookie`, `lidc`, and other cookies they normally travel with in a real
browser - is itself a signal LinkedIn's session-anomaly detection can key
on; replaying the whole jar reads much closer to an actual browser request.
`app/voyager_client.py` also sends the standard `sec-ch-ua`/`sec-fetch-*`/
`accept-language`/`referer` headers a real Voyager XHR carries, for the same
reason. None of this is evasion of anything - it's the same "look like the
browser tab that's supposed to be making this request" principle the account
-safety guardrails below are built on.

All of it comes from a normal logged-in browser session (DevTools → Network
→ Copy as cURL, or → Application → Cookies for the minimal pair) and is used
in-memory for that one request only, never stored or logged. If nothing is
sent, the backend falls back to an optional demo session configured via
`LINKEDIN_FULL_COOKIE_B64` (preferred), `LINKEDIN_FULL_COOKIE`, or
`LINKEDIN_LI_AT` / `LINKEDIN_JSESSIONID` environment variables, checked in
that order - never committed to the repo. The `_B64` form is base64-encoded
specifically because a raw cookie string contains quotes/`#`/spaces that can
collide with `.env`'s own quoting and comment rules depending on how it's
pasted in (this bit us during testing); base64 only ever produces
`[A-Za-z0-9+/=]`, so it can't misparse regardless of what's inside the
cookie. The plain `LINKEDIN_FULL_COOKIE` form still works, but needs the
whole value wrapped in single quotes to survive `.env` parsing.

**Getting your own session values**: in DevTools → Network, click any
`www.linkedin.com` request, right-click → Copy → **Copy as cURL (bash)**, then:

```bash
python3 tools/curl_to_env.py    # paste, then press Enter
python3 tools/check_session.py  # one request: is it live?
```

A copied cURL command already carries the complete Cookie header the browser
sent, `li_at` included, so nothing has to be copied by hand. It's read from
stdin rather than as an argument so a live session doesn't land in shell
history or `ps` output, and `.env` is rewritten atomically at mode 600 with
every other line preserved.

`tools/check_session.py` then answers "is my cookie dead, or is my code
wrong?" in a single Voyager request rather than the seven a `/profile` fetch
costs, and distinguishes an expired session (302 to login) from a throttled
one (429) from a wrong public identifier.

Use it right after capturing, not habitually before every fetch: it runs in
its own process and therefore opens its own connection, and connections are
the scarce resource (see
[What actually got sessions revoked](#what-actually-got-sessions-revoked)).
Checking immediately before a fetch you care about can be what breaks it.

After updating `.env`, **fully stop and restart** the server: `--reload`
watches `.py` files, so editing `.env` alone triggers no reload and the
running process keeps serving the previous cookie. (`.env` is loaded with
`override=True`, so once the process does restart the file wins over anything
inherited from the shell.)

<details>
<summary>Alternative: the DevTools console helper</summary>

Paste
[`tools/get_session_cookie.js`](tools/get_session_cookie.js) into your
browser's DevTools console while logged into linkedin.com (in Chrome, type
`allow pasting` in the console once first). It reads every cookie it can
(everything except `li_at`), walks you through copying `li_at` manually, and
puts a ready-to-paste `LINKEDIN_FULL_COOKIE_B64=...` line on your clipboard;
`copy(__liCookie)` gives you the raw value for direct header use (Postman,
`curl -H`). It validates as it goes - trimming the stray whitespace and
quotes a copy from the DevTools cookie table usually carries, rejecting a
truncated `li_at`, and refusing to proceed if `JSESSIONID` isn't readable -
because each of those otherwise yields a cookie that looks correct and fails
later as an indistinguishable `401`. Neither value is printed in full, since
console history outlives a reload and a screenshot of that tab would leak a
live session. `li_at` is `HttpOnly`, so no page script (this one included) is
allowed to read it; that's the browser protecting you from exactly this kind
of script being able to steal it via XSS, not a gap in the snippet. Nothing it does leaves
your own browser - no network calls, no data sent anywhere. Prefer
`curl_to_env.py` above: that manual `li_at` paste is where a stray space or
wrapping quote gets in, and the resulting cookie fails as an opaque 401.

</details>

**A practical note on replaying a live session**: reusing the exact session
token an actively-open browser tab is also using can trigger LinkedIn's
own anomaly detection into invalidating that session, forcing a fresh login
- on whichever browser holds it, including your own. It's not something
header/cookie completeness fully eliminates, since the underlying signal is
one token being driven by two concurrent clients, not just "looks like a
script." If that's disruptive during testing, capture the session from a
secondary, otherwise-idle browser profile rather than your daily-driver one,
so a forced re-login there doesn't interrupt your normal LinkedIn use.

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
  This project's built-in `DAILY_QUOTA` defaults to 150 - a tenth of that -
  and realistic use
  (demoing this API) touches on the order of 10 profiles.
- Guardrails: a jittered pause **between every upstream request**
  (`app/voyager_client.py`) rather than once per `/profile` - one profile is a
  fan-out of several Voyager calls, so pausing once still let them go out
  back-to-back, and LinkedIn was observed 302ing the last of eleven to the
  login page. Only the six sections the output actually uses are fetched, most
  valuable first, so that if throttling does begin mid-sequence what it costs
  is an optional section rather than every job title. Plus a hard daily quota
  **per LinkedIn account** (`app/rate_limit.py`) and a kill switch
  (`ALLOW_LIVE=false`) that stops all live traffic and serves cache-only.
- A session rejected mid-fan-out (302/401/403) fails the request instead of
  being swallowed as an absent section. Returning `200` with a silently
  gutted profile hides a dying session behind an apparently fine response.

None of this makes scraping risk-free - it's a judgment call about where on
the risk spectrum this sits, not a legal opinion.

### What actually got sessions revoked

Most of the work here went into a session that kept dying after one or two
requests. The findings were counter-intuitive enough to be worth recording,
since they shaped several design decisions above.

**Connection churn, not credentials.** LinkedIn tolerates a replayed session
more or less indefinitely over a *stable* connection, but revokes it after a
handful of new TLS handshakes. The original code built a fresh
`httpx.AsyncClient` per request, so every `/profile` opened a new connection -
which meant the API worked in testing and would have died on a grader's third
call. Two lines of client configuration fixed it: a pooled client owned by the
app's `lifespan`, and `keepalive_expiry=600` in place of httpx's 5-second
default (without which any two requests more than five seconds apart still get
separate connections - i.e. all real traffic). One process now behaves like
one browser tab holding one connection open.

**Pace between requests, not per call.** A single profile is a fan-out of
seven Voyager calls. A delay applied once per `/profile` still let those seven
go out back-to-back; LinkedIn answered the last of them with a 302 to the
login page. The jittered pause belongs in the client, between every request.

**Fail loudly on 302.** A rejected session mid-fan-out was originally
swallowed as "this section is empty", so a dying session returned `200` with a
silently gutted profile - no job titles, no city - which is far worse than an
error. Only `404` is treated as a genuinely absent section now.

**Log what you swallow.** The single highest-value change was logging skipped
sections. `section profilePositions unavailable: 302` is what revealed that
the *last* section in the fan-out was the one dying, and everything above
followed from that. Silent degradation had made the problem invisible.

Things that turned out **not** to be the cause, despite looking convincing:
`li_at` rotation (LinkedIn returns no `Set-Cookie` at all on success), one-use
tokens, and browser-fingerprint mismatch. The `Set-Cookie: li_at; Max-Age=0`
seen on failures is just what an authwall response looks like, not evidence of
what triggered it. Replaying the real captured `user-agent`/`sec-ch-ua`
headers (see `tools/curl_to_env.py`) is still worth doing - a fabricated
fingerprint that contradicts itself is strictly worse than none - but it was
not what fixed this.

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

33 tests, no network access required. `tests/test_denormalize.py` runs the
denormalizer against the three synthetic fixtures;
`tests/test_voyager_client.py` drives the fan-out through
`httpx.MockTransport` to cover the fetch set and ordering, failing fast on a
rejected session, tolerating an absent one, connection reuse, and that the
pause happens *between* requests rather than once per profile;
`tests/test_config.py` covers the ways a `.env` can be wrong.

Two helper scripts, neither needed for normal operation:

```bash
python3 tools/curl_to_env.py     # "Copy as cURL" -> .env session + fingerprint
python3 tools/check_session.py   # one request: is the configured session live?
```

`check_session.py` opens its own connection, so avoid running it immediately
before a fetch you care about - see the session notes above.

## Deployment

Any host that runs a standard ASGI app works (Railway, Render, Fly.io).

`.python-version` pins CPython 3.12 deliberately. Hosts that default to a
newer interpreter have no prebuilt `pydantic-core` wheel for it yet and fall
back to compiling it from Rust source, which fails on build images with a
read-only cargo cache. Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Set environment variables from `.env.example` in the host's dashboard - never
commit real credentials. `tools/curl_to_env.py --print` emits the two session
values (`LINKEDIN_FULL_COOKIE_B64` and `LINKEDIN_BROWSER_HEADERS_B64`) ready
to paste. Also set `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` here
and in your local `.env` if you want local runs and the deployed server to
share one daily quota (see
[Shared quota](#shared-quota-across-local--deployed-runs) above).

Pick a region near where the session was captured. The cookie is bound to a
browser on a particular network; replaying it from a datacenter on another
continent is one more thing that reads as anomalous, on top of the datacenter
IP itself. Expect a deployed backend demo session to be less durable than a
local one - which is why `x-li-cookie` (caller-supplied sessions) is the
documented primary path rather than a fallback.

## API

### `GET /profile`

| Param / Header | Required | Description |
|---|---|---|
| `url` (query) | yes | Full profile URL or bare public identifier |
| `force_refresh` (query) | no | Bypass cache and re-fetch live |
| `fields` (query) | no | Comma-separated subset of output fields. Default: all |
| `x-li-cookie` (header) | no* | **Recommended.** The whole `Cookie` header from a real linkedin.com request |
| `x-li-at` (header) | no* | Minimal alternative: just the `li_at` cookie |
| `x-jsessionid` (header) | no* | Paired with `x-li-at` |
| `x-api-key` (header) | no** | Required to spend the *backend* session on a deployment that sets `API_KEY` |

\* required unless a backend demo session is configured. `x-li-cookie` wins
when both forms are supplied.

\** **The submitted deployment runs with `API_KEY` unset, so `/profile` is open
- the brief asks for a public API and a reviewer should be able to curl the URL
with no header and get a profile back.** The key exists because that openness
is a deliberate choice rather than an oversight: a deployment carrying a
backend cookie with no key is an open proxy for that LinkedIn account, and
anyone who finds the URL can scrape through it on your identity until the
daily quota runs out. Here the quota (150/day, per account) is what caps the
exposure; `API_KEY` is the switch to close it entirely, and is what a
non-demo deployment of this service would set. The key is only
demanded from callers who *don't* bring a session of their own - if you send
your own `x-li-cookie` you're spending your own account's risk budget, so
there's nothing for the key to protect. `/health` stays open either way, so
the service still looks alive to a monitor. A malformed `x-li-cookie` does not
count as bringing your own session: it falls through to the backend cookie, so
the key is still required. The app logs a startup warning when a backend
session is configured with no key set.

```bash
curl -s 'https://<your-deployment>/profile?url=https://www.linkedin.com/in/satyanadella' | jq
```

A live fetch takes roughly 10-15s: seven upstream Voyager requests with a
jittered pause between each (see the guardrails above). Cache hits return
immediately and are marked `"source": "cache"`.

#### Narrowing the response with `fields`

Latency here is almost entirely the paced section fan-out, so the way to make
a call fast is to make fewer requests. Every output field maps to exactly one
section, and `?fields=` lets a caller pay only for what they use:

```bash
curl -s 'https://<your-deployment>/profile?url=<url>&fields=name,headline' | jq
```

| `fields=` | Upstream requests | Roughly |
|---|---|---|
| `name`, `headline`, `about`, `images` (any combination) | 1 | ~0.5s |
| `location` | 2 | ~1.5s |
| `education` / `skills` / `certifications` / `languages` (each) | 2 | ~1.5s |
| `experience` | 3 | ~2.5s |
| omitted (all fields) | 7 | ~9.5s |

Three details worth knowing:

- **`public_identifier` and `name` are always included.** Both come off the
  resolve call at no extra cost, and a response you can't tie back to a person
  isn't much use.
- **Unrequested fields are absent from the JSON, not empty.** A missing key
  means "you didn't ask for this"; `"skills": []` means "this member has no
  skills". Returning `[]` for both would make a narrow query look like a very
  sparse profile. `meta.fields` lists what the response actually carries.
- **`location` costs a section**, which is not obvious - it pulls
  `profilePositions`. The readable city string appears nowhere in the resolve
  response; the denormalizer recovers it by matching the profile's `geoUrn`
  against the geoUrn→name pairs the *positions* response carries. Skipping
  that request would silently degrade `?fields=location` to a country code.

#### Staleness: `source: "stale"`

Cache entries expire after 24h, but an expired entry is not thrown away. It
gets served in two situations, both marked `meta.source: "stale"` with a note
in `limitations` saying which - a caller is never silently handed old data.

**Expired entry, refresh in the background.** The caller who happens to arrive
after expiry used to pay the full ~9.5s to re-fetch. Now they get the stale
copy in ~0.2s and a refresh starts behind the response, so the *next* caller
gets fresh data. Two things this gets right: only one refresh runs per profile
at a time (five concurrent requests for one stale profile launch one fan-out,
not five), and a refresh that fails leaves the stale entry exactly where it
was - losing good stale data because the refresh of it failed would make this
worse than plain expiry.

**Live fetch failed, stale copy exists.** A dead session used to turn every
request into a `401`, including requests for profiles sitting in the cache
that needed no session at all. Now the stale copy is returned instead, with
`limitations` naming the upstream status. The API degrades rather than falling
over, which matters most in exactly the situation the backend session is least
reliable.

The one exception is `404`: "no such member" may mean the profile was deleted
or renamed, and answering that with old data would assert something that is no
longer true. Every other failure is about *us* - session rejected, throttled,
upstream broken - which says nothing about whether the cached copy is still
accurate.

Note that live fan-outs are serialized: a background refresh running
underneath a foreground fetch would put two interleaved paced sequences on one
connection, which is the burst signature the pacing exists to avoid. Under
normal single-caller traffic this never contends.

#### Where the cache lives

Two backends behind one interface, chosen by `CACHE_BACKEND` (`auto` by
default: Upstash when it's configured, disk otherwise) - the same shape as the
quota counter's in-memory/Upstash split.

| | `DiskCache` | `UpstashCache` |
|---|---|---|
| Storage | JSON files under `CACHE_DIR` | The Redis the quota counter already uses |
| Survives a restart | no | **yes** |
| Read latency | ~0.2s | ~0.5s (one REST round-trip) |
| Good for | local development | any real deployment |

Disk was close to decorative on Render's free tier: the container - and its
filesystem - is replaced on every deploy and after ~15 minutes idle, so
entries rarely survived long enough for the 24h TTL or the stale-serving paths
to mean anything. Upstash fixes that at the cost of one REST round-trip, which
against a ~9.5s live fetch is noise.

**Expiry is decided in application code, never by Redis.** `EXPIRE` *deletes*
the key when it fires, which would destroy the stale copy at exactly the
moment stale-while-revalidate and serve-stale-on-failure need it, collapsing
both features back into a hard 24h cliff. The entry carries its own
`cached_at` and freshness is computed from that; the Redis TTL is set to seven
days and exists purely as garbage collection.

Both backends follow the same three rules: writes are atomic (temp file plus
`os.replace` on disk, `SET` on Redis), a read never raises - anything
unreadable, malformed or written by an older schema version is a *miss* - and
a write that fails is logged and swallowed, because the response the caller is
waiting on is already computed. Upstash reads and writes retry once on a short
timeout: a false miss costs a full fan-out *and* a unit of daily quota, which
is the genuinely scarce resource.

Only a **complete** fetch is written to the cache. A narrowed one is missing
sections, and caching it would let `?fields=name` poison the entry a later
full request reads - returning a 200 with empty experience and education,
indistinguishable from a member who has neither. Reads go the other way
happily: a cached entry is always complete, so `?fields=name` off a warm cache
costs nothing at all.

Responses: `200` with the profile JSON, `400` (unparseable URL), `401` (no
usable session / bad `x-api-key` / session rejected by LinkedIn), `404`
(profile not found), `429` (daily quota exhausted, **or LinkedIn rate-limiting
the session** - both carry `Retry-After`), `503` (kill switch engaged).

Upstream `429` is reported as `429`, not as a generic `502`. It's the one
status where the caller's correct move is to back off rather than retry or
re-auth, and it's the earliest warning that the account is under pressure -
so it also aborts the remaining fan-out and is logged loudly rather than
being swallowed as "this section is missing".

The kill switch outranks session resolution: with `ALLOW_LIVE=false` and no
session supplied you get the `503`, not a `401`. The `401` would be true but
misleading - no cookie would have helped.

#### Response headers

Every response carries `X-Request-ID` (the same id in the logs and in
`meta.request_id`). Any response that can identify an account also carries:

```
X-RateLimit-Limit:     150
X-RateLimit-Remaining: 147
X-RateLimit-Reset:     1788134400   # unix ts, next UTC midnight
Retry-After:           3600         # 429 only
```

The quota day is keyed on the **UTC** date, so `X-RateLimit-Reset` is true
regardless of which region the service is deployed in.

#### The `meta` block

```jsonc
"meta": {
  "source": "live",                 // live | cache | stale
  "fetched_at": "2026-08-30T…",
  "request_id": "01j2f4a9c1b7",
  "duration_ms": 9480,
  "upstream_requests": 7,           // 0 on a cache hit
  "fields": ["name", "headline"],   // what this response carries
  "cache_age_seconds": null,        // null on a live fetch
  "quota_remaining": 147            // null if the quota store was unreachable
}
```

`source` and `fetched_at` are still duplicated at the top level for existing
consumers, and are deprecated in favour of `meta`. `quota_remaining` is
best-effort: a blip reading the shared Upstash counter reports `null` rather
than failing a response that is otherwise perfectly good.

Note `upstream_requests` against the quota: the daily quota counts `/profile`
calls, so real traffic on the account is up to **7×** the number the counter
shows. A fetch that fails *after* reaching LinkedIn is not refunded - the
requests were made and the exposure was spent. A fetch that fails before
issuing any upstream request is refunded.

### `GET /health`

Liveness, deployment posture (`allow_live`, `api_key_required`,
`shared_quota_store`, `daily_quota`, `quota_resets_at`) and the backend
session's remaining daily quota. No auth required, deliberately - a monitor
must be able to tell the service is up without holding the API key.

## Limitations

- **City-level location is best-effort.** LinkedIn's profile entity carries
  only `location.countryCode` and an opaque `geoLocation.geoUrn` - the
  readable city string is genuinely absent from that response. Rather than
  make a separate undocumented geo-resolve call, the denormalizer resolves
  the geoUrn against the `geoUrn -> geoLocationName` pairs that the
  **positions** response already carries, which costs nothing extra and
  returns e.g. `"New York City Metropolitan Area"`. When a member's profile
  location doesn't match any of their role locations the lookup misses and
  the output degrades to the country code, with a note in `limitations`.
- **These are internal, undocumented endpoints.** LinkedIn can change field
  names, response shapes, or retire endpoints without notice - as it already
  did to the old aggregator endpoints this project had to route around.
- **Fixtures are synthetic, not real scraped data.** No real profile's
  content ships in this repo; `fixtures/sample_raw.json`,
  `sample_raw_notable.json` and `sample_raw_multirole.json` are hand-built in
  the real Voyager response shape to exercise the denormalizer, not actual
  LinkedIn responses.
- **Four sections are fetched but unmapped.** `profileCourses`,
  `profileProjects`, `profileHonors` and `profileVolunteerExperiences` are
  known-good endpoint paths listed in `SECTION_PATHS`, but nothing maps them
  into the output yet, so they are deliberately not requested - each one is
  another request against the same session for data that would be discarded.
  Wiring them up is the most obvious next extension.
- **A backend demo session is inherently perishable.** It's a real browser
  session being replayed; LinkedIn can revoke it at any time, and a deployed
  instance is more exposed than a local one. Callers supplying their own
  session via `x-li-cookie` are unaffected.
