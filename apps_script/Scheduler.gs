/**
 * UAE Job Radar — reliable scheduler (Google Apps Script).
 *
 * GitHub's built-in cron has been firing far less often than configured since late Aug 2026
 * (an hourly job may run only 2-3 times a day). This script presses the "Run workflow" button
 * for you through GitHub's API on Google's own, reliable timer:
 *   poll   every hour
 *   sweep  daily around 04:00 IST
 *   digest Mon / Wed / Fri around 08:00 IST
 * GitHub's cron stays on as a backup; duplicate runs are harmless (the digest sends once a day).
 *
 * Setup (5 min): README → "Reliable schedule".
 *   1. Create a fine-grained GitHub token for this repo with  Actions: Read and write.
 *   2. Project Settings → Script properties → add GITHUB_TOKEN.
 *   3. Check RADAR below, then run setupSchedulerTriggers() once and approve.
 */

const RADAR = { owner: 'hafiz-hilalhumaid', repo: 'uae-job-radar', ref: 'main' };

function runPoll() { dispatch_('poll.yml'); }
function runSweep() { dispatch_('sweep.yml'); }
function runDigest() {
  const day = Utilities.formatDate(new Date(), 'Asia/Kolkata', 'EEE');
  if (['Mon', 'Wed', 'Fri'].indexOf(day) !== -1) dispatch_('digest.yml');
}

function dispatch_(workflow) {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error('Add GITHUB_TOKEN in Project Settings → Script properties.');
  const url = `https://api.github.com/repos/${RADAR.owner}/${RADAR.repo}/actions/workflows/${workflow}/dispatches`;
  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    muteHttpExceptions: true,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ ref: RADAR.ref }),
  });
  if (res.getResponseCode() !== 204) {
    // Apps Script e-mails you about failed triggers, e.g. when the token expires.
    throw new Error(`GitHub refused to start ${workflow}: HTTP ${res.getResponseCode()} ${res.getContentText().slice(0, 300)}`);
  }
}

/** Run once. Replaces only this file's triggers (the e-mail forwarder's triggers are left alone). */
function setupSchedulerTriggers() {
  const mine = ['runPoll', 'runSweep', 'runDigest'];
  ScriptApp.getProjectTriggers()
    .filter(t => mine.indexOf(t.getHandlerFunction()) !== -1)
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('runPoll').timeBased().everyHours(1).create();
  ScriptApp.newTrigger('runSweep').timeBased().everyDays(1).atHour(4).inTimezone('Asia/Kolkata').create();
  ScriptApp.newTrigger('runDigest').timeBased().everyDays(1).atHour(8).inTimezone('Asia/Kolkata').create();
}
