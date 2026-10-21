/* Real browser UI driver. Requires Playwright (not @playwright/test).
 * node tools/browser_acceptance_runner.cjs URL VARIANT OUTPUT_DIR pilot|cases
 * PLAYWRIGHT_MODULE can point to an already installed Playwright module.
 * No HTTP/API prompt injection: all questions go through the visible composer.
 */
const fs = require('fs');
const path = require('path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const [url, variant, outputArg, batch = 'cases'] = process.argv.slice(2);
const output = path.resolve(outputArg);
const casesFile = path.join(__dirname, 'browser_acceptance_cases.json');
const all = JSON.parse(fs.readFileSync(casesFile, 'utf8'));
let cases = all[batch];
if (batch !== 'pilot' && variant !== 'D') cases = cases.filter(c => c.compare);
if (process.env.SAGE_CASE_IDS) cases = cases.filter(c => process.env.SAGE_CASE_IDS.split(',').includes(c.id));
fs.mkdirSync(output, {recursive: true});
const journal = path.join(output, `${variant}-journal.jsonl`);
const records = path.join(output, `${variant}-browser.jsonl`);
function rows(file) {
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean).flatMap(l => {try {return [JSON.parse(l)];} catch {return [];}});
}
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  const browser = await chromium.launch({headless: true, channel: 'chrome'});
  try {
    for (const test of cases) {
      // New synthetic local identity for each case; turns within a chain share it.
      const context = await browser.newContext({viewport: {width: 1440, height: 1000}, locale: 'zh-CN'});
      const page = await context.newPage();
      const errors = [];
      let taskEnds = 0;
      page.on('pageerror', e => errors.push(e.name + ': ' + e.message));
      page.on('websocket', ws => ws.on('framereceived', ({payload}) => {
        if (typeof payload === 'string' && payload.startsWith('42')) {
          try {if (JSON.parse(payload.slice(2))[0] === 'task_end') taskEnds++;} catch {}
        }
      }));
      try {
        await page.goto(url);
        await page.locator('#chat-settings-header-button').waitFor({state: 'visible', timeout: 30000});
        await page.locator('#chat-input').waitFor({state: 'visible'});
        for (let i = 0; i < test.turns.length; i++) {
          const id = `${variant}-${test.id}-t${i + 1}`;
          const before = rows(journal).length;
          const ends = taskEnds;
          const start = Date.now();
          console.log(JSON.stringify({event: 'start', id, at: new Date().toISOString()}));
          await page.locator('#chat-input').fill(test.turns[i]);
          await page.locator('#chat-submit').click();
          let row;
          while (Date.now() - start < 200000) {
            row = rows(journal).slice(before).find(r => r.question === test.turns[i].split('```')[0].trim() ||
              r.question === test.turns[i] || (test.turns[i].includes('```') && test.turns[i].startsWith(r.question)));
            if (row && taskEnds > ends) break;
            await delay(500);
          }
          if (!row) throw new Error('No correlated server journal before 200s timeout');
          await page.locator('#stop-button').waitFor({state: 'hidden', timeout: 10000});
          await delay(300);
          const dom = await page.evaluate(() => ({
            text: document.querySelector('main')?.innerText || document.body.innerText,
            messages: Array.from(document.querySelectorAll('.message-content'), e => e.innerText),
            math: {inline: document.querySelectorAll('.katex').length, display: document.querySelectorAll('.katex-display').length, errors: document.querySelectorAll('.katex-error').length},
            width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
          }));
          const record = {
            id, variant, case_id: test.id, turn: i + 1, kind: test.kind,
            expected: test.expected, submitted_prompt: test.turns[i],
            at: new Date().toISOString(), url: page.url(), thread_id: row.thread_id,
            browser_seconds: (Date.now() - start) / 1000, task_end_seen: taskEnds > ends,
            page_errors: errors.slice(), journal_row: row, dom,
            screenshot: `${id}.png`,
          };
          fs.writeFileSync(path.join(output, `${id}.json`), JSON.stringify(record, null, 2));
          fs.appendFileSync(records, JSON.stringify(record) + '\n');
          await page.screenshot({path: path.join(output, `${id}.png`), fullPage: true});
          console.log(JSON.stringify({event: 'done', id, seconds: record.browser_seconds,
            audit: row.response?.audit?.status, calls: row.response?.call_trace?.length,
            task_end_seen: record.task_end_seen}));
        }
      } catch (error) {
        const id = `${variant}-${test.id}-browser-error-${Date.now()}`;
        fs.writeFileSync(path.join(output, `${id}.json`), JSON.stringify({id, case_id:test.id, variant,
          error: error.message, at:new Date().toISOString(), url:page.url()}, null, 2));
        await page.screenshot({path:path.join(output, `${id}.png`), fullPage:true}).catch(() => {});
        console.error(JSON.stringify({event:'error', id, error:error.message}));
        process.exitCode = 1;
      } finally {await context.close();}
    }
  } finally {await browser.close();}
})();
