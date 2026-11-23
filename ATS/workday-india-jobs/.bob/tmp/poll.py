import httpx,sys
sys.path.insert(0,'.')
import workday_india_jobs as w
h={"User-Agent":w.UA,"Accept":"application/json","Content-Type":"application/json","Accept-Encoding":"identity"}
x=httpx.post("https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/jobs",json={"appliedFacets":{},"limit":1,"offset":0,"searchText":""},headers=h,timeout=30)
print(__import__('time').strftime('%H:%M:%S'),x.status_code,x.headers.get('cf-mitigated'))
