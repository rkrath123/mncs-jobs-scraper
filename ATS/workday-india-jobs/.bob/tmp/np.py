import json,os,asyncio,sys,re
sys.path.insert(0,'.')
import workday_india_jobs as w
C=os.path.expanduser('~/.cache/mncs-jobs-scraper/')
recs={}
for l in open(C+'audit_probes.jsonl'):
    r=json.loads(l); recs[r['key']]=r
np_=[recs[k] for k in json.load(open(C+'audit_cand.json'))['noplace']]
async def main():
    sc=w.Scraper(["x"],"title",8,30.0,False,rate=8); out=[]
    async def one(r):
        res=[]
        for t in ["India","Bangalore","Hyderabad","Pune"]:
            resp,why=await sc._request("POST",f"{r['api']}/jobs",json={"appliedFacets":{},"limit":20,"offset":0,"searchText":t})
            if resp is None: res.append((t,'ERR',why)); continue
            d=resp.json(); res.append((t,d.get('total'),[p.get('locationsText') for p in d.get('jobPostings',[])]))
        out.append((r['name'],res))
    await asyncio.gather(*(one(r) for r in np_)); await sc.close()
    err=sum(1 for n,res in out for x in res if x[1]=='ERR')
    totals=sum((x[1] or 0) for n,res in out for x in res if x[1]!='ERR')
    multi=[(n,x[0],x[2]) for n,res in out for x in res if x[1]!='ERR' and x[1] and any(re.search(r'\d+ Locations',l or '') for l in x[2])]
    print('sites',len(out),'errors',err,'total postings returned by India/city text searches:',totals)
    for n,t,l in multi[:20]: print('  multi-location listing',n,t,[x for x in l if x][:3])
    nz=[(n,x[0],x[1]) for n,res in out for x in res if x[1] not in ('ERR',0,None)]
    for n,t,c in nz[:30]: print('  nonzero',n,t,c)
asyncio.run(main())
