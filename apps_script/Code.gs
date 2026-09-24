/**
 * UAE Job Radar — job-board alert forwarder (Google Apps Script).
 *
 * LinkedIn, Indeed, Bayt, GulfTalent, Naukrigulf, Google Jobs, Dubai Careers and
 * HiringCafe don't offer job-seeker APIs, and scraping them breaks their terms.
 * Their own e-mail alerts are allowed, so this script reads those e-mails, pulls
 * out job links whose titles match your target roles, and forwards new ones to
 * the same Telegram chat as the radar.
 *
 * Setup (5 min): see "Board alerts" in README.md.
 *   1. Gmail: create the label below and a filter that applies it to alert e-mails.
 *   2. script.google.com -> New project -> paste this file.
 *   3. Project Settings -> Script properties: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
 *   4. Run setupTriggers() once and approve the permissions.
 */

const CONFIG = {
  label: 'job-alerts',
  maxThreads: 50,
  include: /(front[\s-]?end|react|typescript|next\.?js|javascript|full[\s-]?stack|software (engineer|developer)|\bsde\b|\bswe\b|web (developer|engineer)|ui (engineer|developer)|\bai\b|agentic|\bllm\b|gen ?ai|forward deployed|product engineer)/i,
  exclude: /(\bintern|trainee|graduate|\bqa\b|\btest|devops|\bsre\b|android|\bios\b|flutter|salesforce|\bsap\b|support|sales|business develop|manager|director|designer|analyst|emirati|uae national)/i,
};

// Known job-link shapes -> a stable key so the same job isn't forwarded twice.
// Best-effort: check the links in your first few alert e-mails and adjust if needed.
const LINK_PATTERNS = [
  { site: 'LinkedIn',   re: /linkedin\.com\/(?:comm\/)?jobs\/view\/(\d+)/i, canon: id => `https://www.linkedin.com/jobs/view/${id}` },
  { site: 'Indeed',     re: /indeed\.com\/[^"'\s]*?[?&]jk=([a-f0-9]{8,})/i, canon: id => `https://ae.indeed.com/viewjob?jk=${id}` },
  { site: 'Bayt',       re: /bayt\.com\/[^"'\s]*?jobs\/[^"'\s]*?-(\d{6,})/i, canon: null },
  { site: 'GulfTalent', re: /gulftalent\.com\/[^"'\s]*?jobs\/[^"'\s]*?-(\d{5,})/i, canon: null },
  { site: 'Naukrigulf', re: /naukrigulf\.com\/[^"'\s]*?(\d{8,})/i, canon: null },
  { site: 'Google',     re: /google\.[a-z.]+\/search\?[^"'\s]*?(?:htidocid|docid)=([A-Za-z0-9_%=-]+)/i, canon: null },
];

function forwardJobAlerts() {
  const label = GmailApp.getUserLabelByName(CONFIG.label);
  if (!label) throw new Error(`Create a Gmail label named "${CONFIG.label}" first.`);
  const props = PropertiesService.getScriptProperties();
  const fresh = [];

  label.getThreads(0, CONFIG.maxThreads).forEach(thread => {
    thread.getMessages().forEach(msg => {
      if (!msg.isUnread()) return;
      const sender = senderName_(msg.getFrom());
      extractJobs_(msg.getBody()).forEach(job => {
        const key = 'seen_' + hash_(job.key);
        if (props.getProperty(key)) return;
        props.setProperty(key, String(Date.now()));
        job.sender = sender;
        fresh.push(job);
      });
      msg.markRead();
    });
  });

  if (fresh.length) sendTelegram_(fresh);
}

function extractJobs_(bodyHtml) {
  const out = [];
  const seenHere = {};
  const anchor = /<a\b[^>]*?href\s*=\s*"([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi;
  let m;
  while ((m = anchor.exec(bodyHtml)) !== null) {
    const href = m[1].replace(/&amp;/g, '&');
    const text = m[2].replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&')
      .replace(/&#39;/g, "'").replace(/\s+/g, ' ').trim();
    if (!text || text.length > 200) continue;
    if (!CONFIG.include.test(text) || CONFIG.exclude.test(text)) continue;

    let site = 'Alert', key = href.split('#')[0], url = href;
    for (const p of LINK_PATTERNS) {
      const hit = href.match(p.re);
      if (hit) {
        site = p.site;
        key = `${p.site}:${hit[1]}`;
        if (p.canon) url = p.canon(hit[1]);
        break;
      }
    }
    if (seenHere[key]) continue;
    seenHere[key] = true;
    out.push({ key, site, title: text, url });
  }
  return out;
}

function sendTelegram_(jobs) {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('TELEGRAM_BOT_TOKEN');
  const chatId = props.getProperty('TELEGRAM_CHAT_ID');
  if (!token || !chatId) throw new Error('Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Script properties.');

  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  for (let i = 0; i < jobs.length; i += 20) {
    const batch = jobs.slice(i, i + 20);
    const lines = [`📨 <b>${jobs.length} new from job-board alerts</b>${jobs.length > 20 ? ` (${i + 1}–${i + batch.length})` : ''}`];
    batch.forEach(j => lines.push(`<a href="${esc(j.url).replace(/"/g, '&quot;')}">${esc(j.title)}</a> · ${esc(j.site === 'Alert' ? j.sender : j.site)}`));
    UrlFetchApp.fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: 'post',
      contentType: 'application/json',
      muteHttpExceptions: true,
      payload: JSON.stringify({ chat_id: chatId, text: lines.join('\n\n'), parse_mode: 'HTML', disable_web_page_preview: true }),
    });
    Utilities.sleep(700);
  }
}

function senderName_(from) {
  const m = String(from).match(/^"?([^"<]+?)"?\s*</);
  return (m ? m[1] : String(from)).trim().slice(0, 40);
}

function hash_(s) {
  const bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, s, Utilities.Charset.UTF_8);
  return Utilities.base64EncodeWebSafe(bytes).slice(0, 22);
}

/** Forget links older than 30 days so Script properties stay small. */
function pruneSeen() {
  const props = PropertiesService.getScriptProperties();
  const cutoff = Date.now() - 30 * 24 * 3600 * 1000;
  const all = props.getProperties();
  Object.keys(all).forEach(k => {
    if (k.indexOf('seen_') === 0 && Number(all[k]) < cutoff) props.deleteProperty(k);
  });
}

/** Run once. Checks the label every 10 minutes and prunes daily. */
function setupTriggers() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('forwardJobAlerts').timeBased().everyMinutes(10).create();
  ScriptApp.newTrigger('pruneSeen').timeBased().everyDays(1).atHour(3).create();
}
