const http=require("http"),{spawn}=require("child_process");
// Real-Chrome check (headless, via the DevTools protocol): applies every scenario in scenarios.json through the
// page's own controls, scrolls like a user, and compares the rows really in the table with the expected count.
// usage: node real_check.js file:///.../india_jobs_report.html scenarios.json
const url=process.argv[2];
const PAGE=`async function(S){
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const $=id=>document.getElementById(id);
const fire=(id,v)=>{const e=$(id);if(e.type==="checkbox")e.checked=v;else e.value=v;e.dispatchEvent(new Event("input"))};
const tbl=()=>$("body").rows.length;
async function settle(){ // scroll to the bottom until every matching row is in the table (or nothing more arrives)
  let last=-1,same=0;
  for(let i=0;i<80&&same<4;i++){window.scrollTo(0,document.body.scrollHeight);await sleep(60);
    if(tbl()===cur.length)break; same=tbl()===last?same+1:0; last=tbl()}
}
const bad=[],pre=[];let n=0;
for(const s of S){
  $("reset").click();const a=s.actions;
  if(a.days!==undefined)[...$("days").children].find(b=>b.dataset.v===a.days).click();
  for(const k of ["undated","match","work","topic","city","ats"])if(a[k]!==undefined)fire(k,a[k]);
  if(a.q!==undefined)fire("q",a.q);
  if(a.company!==undefined)fire("company",a.company);
  await sleep(320);await settle();
  const got={cur:cur.length,rows:tbl(),stat:$("stat").textContent};
  if(got.cur!==s.expected||got.rows!==s.expected||!got.stat.startsWith(s.expected+" of "))bad.push({name:s.name,expected:s.expected,got});
  n++;
}
// stay-at-bottom regression: scroll to the end, change a filter, the new list must fully load without more scrolling
$("reset").click();[...$("days").children].find(b=>b.dataset.v==="0").click();fire("undated",true);await sleep(300);await settle();
fire("ats","workday");await sleep(500);
const wd=cur.length;if(tbl()<Math.min(wd,150+1)||(wd<2000&&tbl()!==wd))bad.push({name:"filter change while scrolled to bottom",cur:wd,rows:tbl()});
// the Company dropdown lists every company in the data, each once, with its job count
const names=[...new Set(DATA.map(x=>x.company))],opts=[...$("company").options].slice(1);
if(opts.length!==names.length||!opts.every(o=>o.textContent===o.value+" ("+DATA.filter(x=>x.company===o.value).length+")"))bad.push({name:"company dropdown",options:opts.length,companies:names.length});
// sorting in the real DOM
const sortRes={};
for(const k of ["t","title","company","loc","m","ats"]){
  $("reset").click();[...$("days").children].find(b=>b.dataset.v==="30").click();fire("undated",true);await sleep(300);
  const th=[...document.querySelectorAll("th")].find(t=>t.dataset.k===k);th.click();await sleep(100);await settle();
  const col={t:0,title:1,company:2,loc:3,m:4,ats:6}[k];
  const v=[...$("body").rows].map(r=>r.cells[col].textContent.trim());
  let ok=true;
  if(k==="t"){const ts=cur.map(x=>x.t===null?-Infinity:x.t);for(let i=1;i<ts.length;i++)if(sortDir===1?ts[i-1]>ts[i]:ts[i-1]<ts[i]){ok=false;break}}
  else{const ks=cur.map(x=>String(x[k]));for(let i=1;i<ks.length;i++)if(ks[i-1].localeCompare(ks[i])*sortDir>0){ok=false;break}}
  if(tbl()!==cur.length)ok=false;
  sortRes[k]=ok;if(!ok)bad.push({name:"sort "+k});
}
// every visible row matches its data (title/company/location/ats text) for the default view
$("reset").click();await sleep(300);await settle();
const rowsOk=[...$("body").rows].every((r,i)=>r.cells[1].textContent===cur[i].title&&r.cells[2].textContent===cur[i].company&&r.cells[3].textContent===cur[i].loc&&r.cells[6].textContent===cur[i].ats);
if(!rowsOk)bad.push({name:"row content"});
return JSON.stringify({summary:{scenarios:n,companyOptions:$("company").options.length-1,mismatches:bad.length,sort:sortRes,rowContentOk:rowsOk,jsErrors:window.__errs||[]},bad});
}`;
const chrome=spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",["--headless=new","--disable-gpu","--window-size=1300,900","--remote-debugging-port=9333","--user-data-dir="+process.cwd()+"/_chrome","about:blank"],{stdio:"ignore"});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const get=u=>new Promise((res,rej)=>http.get(u,r=>{let b="";r.on("data",d=>b+=d);r.on("end",()=>res(JSON.parse(b)))}).on("error",rej));
(async()=>{
 let tabs;for(let i=0;i<40;i++){try{tabs=await get("http://127.0.0.1:9333/json");break}catch(e){await sleep(250)}}
 const t=tabs.find(x=>x.type==="page");const ws=new WebSocket(t.webSocketDebuggerUrl);await new Promise(r=>ws.addEventListener("open",r));
 let id=0;const pend={};ws.addEventListener("message",m=>{m=JSON.parse(m.data);if(pend[m.id]){pend[m.id](m);delete pend[m.id]}});
 const send=(method,params={})=>new Promise(r=>{pend[++id]=r;ws.send(JSON.stringify({id,method,params}))});
 const ev=async e=>{const r=await send("Runtime.evaluate",{expression:e,awaitPromise:true,returnByValue:true});if(r.result.exceptionDetails)throw new Error(JSON.stringify(r.result.exceptionDetails));return r.result.result.value};
 await send("Page.enable");await send("Page.addScriptToEvaluateOnNewDocument",{source:"window.__errs=[];addEventListener('error',e=>__errs.push(e.message))"});await send("Page.navigate",{url});await sleep(3500);
 const SC=require("fs").readFileSync(process.argv[3],"utf8");
 const res=JSON.parse(await ev("("+PAGE+")("+SC+")"));
 console.log(JSON.stringify(res.summary));
 res.bad.slice(0,15).forEach(b=>console.log("MISMATCH",JSON.stringify(b)));
 process.exitCode=res.bad.length?1:0;
 ws.close();chrome.kill();process.exit(0);
})().catch(e=>{console.error(e);chrome.kill();process.exit(1)});
