function buildPageUrl(baseUrl, p) {
  const u = new URL(baseUrl);
  u.searchParams.set('page', String(p));
  u.searchParams.set('mi_ts', String(Date.now()));
  return u.toString();
}

async function openWorkingTab(url) {
  const tab = await chrome.tabs.create({ url, active: false });
  return tab.id;
}

async function waitComplete(tabId, timeoutMs = 60000) {
  return await new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (!done) { done = true; chrome.tabs.onUpdated.removeListener(listener); resolve(false); }
    }, timeoutMs);
    function listener(id, info) {
      if (id !== tabId) return;
      if (info.status === 'complete') {
        clearTimeout(timer);
        if (!done) { done = true; chrome.tabs.onUpdated.removeListener(listener); resolve(true); }
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function gotoAndWait(tabId, url, targetPage){
  await chrome.tabs.update(tabId, { url });
  await new Promise(r=>setTimeout(r, 1500));
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (page) => new Promise(resolve => {
        const pick = () => {
          const labels = Array.from(document.querySelectorAll('.pagination .active, app-pagination .active'));
          const txt = labels.map(x => (x.textContent||'').trim()).join('|');
          if (txt.includes(String(page))) resolve(true); else setTimeout(pick, 250);
        };
        pick();
      }),
      args: [targetPage]
    });
  } catch (e) {
    await new Promise(r=>setTimeout(r, 1500));
  }
}

function isMatch(re, text){ return re.test(text); }

function detectTypeFromTitle(title){
  const t = (title||'').toLowerCase();

  // normalize common blockers first
  if (t.includes('zip hoodie')) return 'ziphoodie';
  if (/\b(pullover\s+hoodie|hoodie)\b/i.test(title)) return 'hoodie';
  if (/\bsweatshirt\b/i.test(title)) return 'sweatshirt';
  if (/\btank\s*top\b/i.test(title)) return 'tanktop';
  if (/\blong\s*sleeve\b/i.test(title)) return 'longsleeve';
  if (/\braglan\b/i.test(title)) return 'raglan';
  if (/\bv[-\s]?neck\b/i.test(title)) return 'vneck';
  if (/\bpremium\s*(t[-\s]?shirt|tee)\b/i.test(title)) return 'premium';

  // SHIRT mapping: T‑Shirt / T Shirt / Shirt / Tee (but NOT Sweatshirt, already excluded)
  if (/\b(t[-\s]?shirt|tee|shirt)\b/i.test(title)) return 'shirt';

  return 'other';
}

async function injectAndScrape(tabId, args) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId },
    func: (args) => (async () => {
      const { mode, scrollPasses, scrollDelay, selectedTypes } = args;
      const sleep = (ms) => new Promise(r=>setTimeout(r,ms));

      function cleanAmazonPngUrl(url) {
        try {
          const u = new URL(url);
          if (u.hostname !== 'm.media-amazon.com') return url;
          const anchor = '/images/I/';
          const path = u.pathname;
          if (!path.includes(anchor)) return url;
          const tail = path.split(anchor, 2)[1];
          const i = tail.toLowerCase().indexOf('.png');
          if (i < 0) return url;
          const up = tail.slice(0, i+4);
          let lastPipe = Math.max(up.lastIndexOf('%7C'), up.lastIndexOf('%7c'));
          let core = lastPipe !== -1 ? up.slice(lastPipe+3) : up.split('/').pop().split('_').pop();
          return `${u.protocol}//${u.host}${anchor}${core}`;
        } catch { return url; }
      }

      function detectTypeFromTitle(title){
        if (title == null) return 'other';
        // blockers
        if (/\bzip\s*hoodie\b/i.test(title)) return 'ziphoodie';
        if (/\b(pullover\s*hoodie|hoodie)\b/i.test(title)) return 'hoodie';
        if (/\bsweatshirt\b/i.test(title)) return 'sweatshirt';
        if (/\btank\s*top\b/i.test(title)) return 'tanktop';
        if (/\blong\s*sleeve\b/i.test(title)) return 'longsleeve';
        if (/\braglan\b/i.test(title)) return 'raglan';
        if (/\bv[-\s]?neck\b/i.test(title)) return 'vneck';
        if (/\bpremium\s*(t[-\s]?shirt|tee)\b/i.test(title)) return 'premium';
        // shirt mapping
        if (/\b(t[-\s]?shirt|tee|shirt)\b/i.test(title)) return 'shirt';
        return 'other';
      }

      async function waitTiles(timeout=40000){
        const start = Date.now();
        while (Date.now()-start < timeout) {
          const n = document.querySelectorAll('app-item-tile').length;
          if (n>0) return true;
          await sleep(200);
        }
        return false;
      }

      async function doScroll(passes=20, delay=250) {
        for (let i=0;i<passes;i++){
          window.scrollBy(0, 1200);
          await sleep(delay);
        }
        window.scrollTo(0,0);
      }

      if (mode === 'infinite') {
        await doScroll(scrollPasses, scrollDelay);
        await waitTiles(40000);
      } else {
        for (let i=0;i<6;i++){ window.scrollBy(0, 800); await sleep(120); }
        window.scrollTo(0,0);
        await waitTiles(40000);
      }

      const tiles = Array.from(document.querySelectorAll('app-item-tile'));
      const rows = [];
      const allow = (selectedTypes||[]);
      for (const t of tiles) {
        const a = t.querySelector('a.name');
        const img = t.querySelector('img');
        if (!a || !img) continue;
        const title = (a.textContent || '').trim();
        const raw = (img.getAttribute('src') || '').trim();
        const image = cleanAmazonPngUrl(raw);
        const asinEl = t.querySelector('.asin .value');
        const asin = asinEl ? (asinEl.textContent || '').trim() : '';
        const type = detectTypeFromTitle(title);
        if (!title || !image) continue;
        if (allow.length === 0 || allow.includes(type)) {
          rows.push({ title, image, asin, type });
        }
      }
      return rows;
    })(),
    args: [args]
  });
  return result || [];
}

function uniqueByKey(items) {
  const map = new Map();
  for (const it of items) {
    const asin = (it.asin || '').trim();
    let key = asin || '';
    if (!key) {
      try { const u = new URL(it.image); key = u.pathname.split('/').pop(); }
      catch { key = `${(it.title||'').toLowerCase()}|${it.image}`; }
    }
    if (!map.has(key)) map.set(key, { title: it.title, image: it.image, asin: asin || undefined, type: it.type });
  }
  return Array.from(map.values());
}

function dedupeTitles(items) {
  const seen = new Map();
  return items.map(it => {
    const base = it.title || 'Untitled';
    const count = (seen.get(base) || 0) + 1;
    seen.set(base, count);
    return { title: count === 1 ? base : `${base} (${count})`, image: it.image, type: it.type };
  });
}

function toDataUrl(str) {
  const b64 = btoa(unescape(encodeURIComponent(str)));
  return `data:application/json;base64,${b64}`;
}

async function downloadJSON(data, filename) {
  try {
    console.log(`[MI v6] Preparing download: ${data.length} items`);
    const out = data.map(({title, image, type}) => ({ title, image, type }));
    const jsonStr = JSON.stringify(out, null, 2);

    try {
      const blob = new Blob([jsonStr], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      await chrome.downloads.download({ url, filename, saveAs: true });
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (e1) {
      console.warn('[MI v6] createObjectURL failed, fallback data URL:', e1);
      const url = toDataUrl(jsonStr);
      await chrome.downloads.download({ url, filename, saveAs: true });
    }
  } catch (err) {
    console.error('[MI v6] Download JSON error:', err);
    try {
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icon.png',
        title: 'Export JSON thất bại',
        message: 'Không thể tạo/tải file JSON. Xem console Service Worker.'
      });
    } catch {}
  }
}

chrome.runtime.onMessage.addListener(async (msg) => {
  if (msg?.type !== 'MI_V6_START') return;

  const { baseUrl, startPage, endPage, infiniteMode, scrollPasses, scrollDelay, selectedTypes } = msg.payload;

  const firstUrl = infiniteMode ? baseUrl : buildPageUrl(baseUrl, startPage);
  const tabId = await openWorkingTab(firstUrl);
  await waitComplete(tabId, 60000);

  let all = [];
  if (infiniteMode) {
    const items = await injectAndScrape(tabId, { mode: 'infinite', scrollPasses, scrollDelay, selectedTypes });
    console.log('[MI v6] Infinite items:', items.length);
    all.push(...items);
  } else {
    for (let p = startPage; p <= endPage; p++) {
      const url = buildPageUrl(baseUrl, p);
      console.log('[MI v6] Navigate ->', url);
      await gotoAndWait(tabId, url, p);
      const items = await injectAndScrape(tabId, { mode: 'paged', scrollPasses, scrollDelay, selectedTypes });
      console.log('[MI v6] Page', p, 'items:', items.length);
      all.push(...items);
    }
  }

  all = uniqueByKey(all);
  all = dedupeTitles(all);
  console.log('[MI v6] Total unique after filter:', all.length);
  await downloadJSON(all, `merchintel_export_filtered.json`);

  try { await chrome.tabs.remove(tabId); } catch {}
});
