import json,os,asyncio,sys,re
sys.path.insert(0,'.')
import workday_india_jobs as w
C=os.path.expanduser('~/.cache/mncs-jobs-scraper/')
recs={}
for l in open(C+'audit_probes.jsonl'):
    r=json.loads(l); recs[r['key']]=r
want=['Booking Holdings','Bronson Healthcare','Unitedliving','Independent Health','UofL Health','Wustl']
async def main():
    sc=w.Scraper(["x"],"title",8,30.0,False,rate=8)
    for r in recs.values():
        if r['name'] not in want or 'err' in r: continue
        for t in ["Bangalore","India"]:
            resp,_=await sc._request("POST",f"{r['api']}/jobs",json={"appliedFacets":{},"limit":20,"offset":0,"searchText":t})
            for p in resp.json().get('jobPostings',[]):
                loc=p.get('locationsText') or ''
                if re.search(r'\d+ Locations',loc) or w.INDIA_LOCATION_RE.search(loc):
                    d,_=await sc._request("GET",f"{r['api']}{p['externalPath']}")
                    i=(d.json().get('jobPostingInfo') or {}) if d else {}
                    locs=[i.get('location')]+list(i.get('additionalLocations') or [])
                    ind=[l for l in locs if l and w.INDIA_LOCATION_RE.search(l)]
                    print(r['name'],'|',t,'|',p.get('title')[:40],'|',loc,'| full:',locs[:4],'| INDIA' if ind else '')
    await sc.close()
asyncio.run(main())
