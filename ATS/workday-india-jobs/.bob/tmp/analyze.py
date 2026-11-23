import json,os,re,collections,sys
sys.path.insert(0,'.')
import workday_india_jobs as w
recs={}
for l in open(os.path.expanduser('~/.cache/mncs-jobs-scraper/audit_probes.jsonl')):
    r=json.loads(l); recs[r['key']]=r
print('sites probed',len(recs))
err=[r for r in recs.values() if 'err' in r]
ok=[r for r in recs.values() if 'err' not in r]
ind=[r for r in ok if r['india']]
non=[r for r in ok if not r['india']]
print('error',len(err),'| india filter found',len(ind),'| dropped as no-India',len(non))
# among dropped: any facet value that smells Indian (any facet label), or no place facet at all
sus=[];noplace=[];empty=[]
for r in non:
    if not r['total']: empty.append(r); continue
    hits=[v for v in r['vals'] if w.INDIA_LOCATION_RE.search(v[2]) or w.INDIA_WORD_RE.search(v[2])]
    if hits: sus.append((r,hits))
    elif not any(w.PLACE_FACET_RE.search(f"{v[0]} {v[1]}") for v in r['vals']): noplace.append(r)
print('dropped with zero postings:',len(empty))
print('dropped but a facet value looks Indian:',len(sus))
for r,h in sus[:40]: print('  ',r['name'],'|',[(x[0],x[2],x[3]) for x in h[:3]])
print('dropped, jobs>0 but NO location-like facet (cannot filter):',len(noplace))
tot=sum(r['total'] or 0 for r in noplace); print('  postings in those sites',tot)
for r in sorted(noplace,key=lambda r:-(r['total'] or 0))[:15]: print('  ',r['name'],r['total'],r['params'])
json.dump({'sus':[r['key'] for r,_ in sus],'noplace':[r['key'] for r in noplace]},open(os.path.expanduser('~/.cache/mncs-jobs-scraper/audit_cand.json'),'w'))
