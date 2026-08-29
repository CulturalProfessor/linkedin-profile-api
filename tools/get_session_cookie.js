/**
 * Session helper for the LinkedIn Profile API.
 *
 * Run this in your browser's DevTools Console while on any linkedin.com page
 * you're logged into. In Chrome you must type `allow pasting` in the console
 * once before it will accept a pasted script.
 *
 * It puts a LINKEDIN_FULL_COOKIE_B64 value on your clipboard and leaves both
 * that and the raw header value on `window` for a second copy. It deliberately
 * does NOT print either in full - console history survives reloads, and a
 * screenshot or screen-share of this tab would hand over a live session.
 *
 * li_at can't be read automatically - it's HttpOnly, a browser security
 * boundary blocking all page JS (this script included) from reading it, to
 * stop exactly this kind of script from stealing it via XSS. No workaround
 * for that; copy it manually as prompted below.
 *
 * Nothing here makes a network request or sends your cookie anywhere.
 */
(function () {
  const otherCookies = document.cookie;
  if (!otherCookies) {
    console.error('No readable cookies found - make sure you are logged into linkedin.com in this tab.');
    return;
  }

  // JSESSIONID is the CSRF token the API sends alongside li_at. It is readable
  // (not HttpOnly), which is the whole reason this approach works - but if it
  // is absent the resulting cookie fails later as an opaque 401, so check now.
  if (!/(^|;\s*)JSESSIONID=/.test(otherCookies)) {
    console.error(
      'JSESSIONID is not present in this page\'s cookies. It is the CSRF token ' +
      'the API needs alongside li_at. Make sure you are on https://www.linkedin.com ' +
      '(not a subdomain) and logged in, reload the page once, then re-run this.'
    );
    return;
  }

  const raw = prompt(
    'Copy li_at from DevTools -> Application -> Storage -> Cookies -> linkedin.com, then paste it here:'
  );
  if (raw === null) return;

  // Copying from the DevTools cookie table routinely picks up surrounding
  // whitespace, and some browsers copy the value already quoted. Either one
  // produces a cookie that looks perfectly fine and fails as a 401 later.
  const liAt = raw.trim().replace(/^"+|"+$/g, '');
  if (!liAt) return;

  if (liAt.length < 100 || /\s/.test(liAt)) {
    console.error(
      `That doesn't look like a complete li_at: got ${liAt.length} characters` +
      `${/\s/.test(liAt) ? ' containing whitespace' : ''}, expected ~150 with none. ` +
      'Copy the entire Value cell - it is long and the column truncates it visually.'
    );
    return;
  }

  const fullCookie = `li_at=${liAt}; ${otherCookies}`;

  // Chunked rather than String.fromCharCode(...bytes): spreading a large array
  // into arguments blows the call-stack argument limit on big cookie jars.
  const toBase64Utf8 = (str) => {
    const bytes = new TextEncoder().encode(str);
    let binary = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(binary);
  };

  const encoded = toBase64Utf8(fullCookie);
  const mask = (s) => `${s.slice(0, 6)}...${s.slice(-4)} (${s.length} chars)`;

  window.__liB64 = encoded;
  window.__liCookie = fullCookie;

  console.log('li_at      :', mask(liAt));
  console.log('cookie jar :', `${otherCookies.split(';').length + 1} cookies, ${fullCookie.length} chars total`);
  console.log('');

  if (typeof copy === 'function') {
    copy(`LINKEDIN_FULL_COOKIE_B64=${encoded}`);
    console.log('Copied to clipboard: the full LINKEDIN_FULL_COOKIE_B64=... line for .env.');
  } else {
    console.log('Run this from the DevTools console to get the value on your clipboard automatically.');
  }

  console.log('');
  console.log('Not printed in full on purpose - console history persists across reloads.');
  console.log('  copy(__liB64)     re-copy the .env value');
  console.log('  copy(__liCookie)  copy the raw value for an x-li-cookie header (Postman, curl -H)');
  console.log('Reload this page when you are done to clear both from memory.');
})();
