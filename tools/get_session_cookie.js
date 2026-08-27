/**
 * Session helper for the LinkedIn Profile API.
 *
 * Run this in your browser's DevTools Console while on any linkedin.com
 * page you're logged into. It reads your own JSESSIONID cookie and builds
 * ready-to-use request headers / a curl command from it.
 *
 * It CANNOT read li_at automatically - that cookie is marked HttpOnly by
 * LinkedIn, which is a deliberate browser security boundary that blocks all
 * page JavaScript (this console included) from reading it, specifically to
 * stop scripts like this one from being able to steal it via XSS. There is
 * no workaround for that from a console snippet, and there shouldn't be.
 * Copy li_at manually instead - see the printed instructions below.
 *
 * Nothing here makes a network request or sends your cookie anywhere; every
 * value stays in your own browser console for you to copy yourself.
 */
(function () {
  const jsessionidRaw = document.cookie
    .split('; ')
    .find((c) => c.startsWith('JSESSIONID='))
    ?.split('=')[1];

  console.log('%cLinkedIn Profile API - session helper', 'font-weight:bold;font-size:14px');

  if (!jsessionidRaw) {
    console.log('Could not find a JSESSIONID cookie - make sure you are logged into linkedin.com in this tab.');
    return;
  }
  const jsessionid = decodeURIComponent(jsessionidRaw);
  console.log('JSESSIONID found:', jsessionid);

  console.log(
    '\nli_at is HttpOnly and cannot be read by any page script - get it manually:\n' +
      '  DevTools -> Application tab -> Storage -> Cookies -> https://www.linkedin.com\n' +
      '  find "li_at" in the list -> copy its Value column.'
  );

  const liAt = prompt('Paste the li_at value you just copied (leave blank to skip):');
  if (!liAt) {
    console.log('\nSkipped - rerun this snippet once you have li_at copied.');
    return;
  }

  console.log('\n%cHeaders for the API:', 'font-weight:bold');
  console.log('x-li-at:', liAt);
  console.log('x-jsessionid:', jsessionid);

  const apiBase = prompt('Deployed API base URL (e.g. https://your-app.up.railway.app), leave blank to skip the example:');
  if (apiBase) {
    console.log(
      '\n%cExample curl:',
      'font-weight:bold',
      `\ncurl -H "x-li-at: ${liAt}" -H "x-jsessionid: ${jsessionid}" "${apiBase.replace(/\/$/, '')}/profile?url=https://www.linkedin.com/in/someone"`
    );
  }
})();
