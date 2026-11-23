import httpx,sys
sys.path.insert(0,'.')
import workday_india_jobs as w
h={"User-Agent":w.UA,"Accept":"application/json","Content-Type":"application/json","Accept-Encoding":"identity"}
for u in ["https://2020companies.wd1.myworkdayjobs.com/wday/cxs/2020companies/external_careers",
          "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite",
          "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site"]:
    x=httpx.post(u+"/jobs",json={"appliedFacets":{},"limit":1,"offset":0,"searchText":""},headers=h,timeout=30)
    print(u.split('/')[2],x.status_code)
