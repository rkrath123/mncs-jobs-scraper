// Drives the generated report in jsdom: applies each scenario's filters through the real UI controls and
// prints the number of rows the page decided to show, plus a few rendering checks.
const { JSDOM } = require("jsdom");
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const scenarios = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const sleep = ms => new Promise(r => setTimeout(r, ms));

const errors = [];
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  beforeParse(w) {
    w.IntersectionObserver = class { observe() {} };
    w.requestAnimationFrame = () => 0;   // jsdom has no layout; the real-browser run is in real_check.js
    w.addEventListener("error", e => errors.push(String(e.message)));
  },
});
const w = dom.window, $ = id => w.document.getElementById(id);
const fire = (id, v) => { const e = $(id); if (e.type === "checkbox") e.checked = v; else e.value = v; e.dispatchEvent(new w.Event("input")); };

(async () => {
  const out = {};
  for (const s of scenarios) {
    $("reset").click();
    const a = s.actions;
    if (a.days !== undefined) [...$("days").children].find(b => b.dataset.v === a.days).click();
    for (const k of ["undated", "match", "work", "topic", "city", "ats"]) if (a[k] !== undefined) fire(k, a[k]);
    if (a.q !== undefined) { fire("q", a.q); await sleep(260); }
    if (a.company !== undefined) { fire("company", a.company); await sleep(260); }
    out[s.name] = { count: w.eval("cur.length"), stat: $("stat").textContent };
  }
  // rendering checks on the default view
  $("reset").click(); fire("undated", true);
  const total = w.eval("cur.length");
  const first = $("body").rows.length;
  w.eval("more()");
  const afterMore = $("body").rows.length;
  const header = [...w.document.querySelectorAll("th")].find(t => t.dataset.k === "t");
  header.click(); const asc = w.eval("cur.map(x=>x.t)");   // first click on the already-sorted-desc column flips to asc
  header.click(); const desc = w.eval("cur.map(x=>x.t)");
  const key = x => x === null ? -Infinity : x;
  const sorted = (arr, dir) => arr.every((v, i) => i === 0 || (dir === 1 ? key(arr[i - 1]) <= key(v) : key(arr[i - 1]) >= key(v)));
  const comp = [...w.document.querySelectorAll("th")].find(t => t.dataset.k === "company");
  comp.click(); const names = w.eval("cur.map(x=>x.company)");
  const companyAscOk = names.every((v, i) => i === 0 || String(names[i - 1]).localeCompare(String(v)) <= 0);
  const na = [...$("body").rows].filter(r => r.cells[0].textContent.trim() === "n/a").length;
  out.__render = { total, firstChunk: first, afterMore, ascOk: sorted(asc, 1), descOk: sorted(desc, -1), companyAscOk, naRowsInView: na,
    firstCell: $("body").rows[0].cells[0].textContent.trim(), jsErrors: errors,
    cityOptions: [...$("city").options].length, headerHasIST: /IST/.test($("body").innerHTML) };
  console.log(JSON.stringify(out));
  process.exit(0);
})();
