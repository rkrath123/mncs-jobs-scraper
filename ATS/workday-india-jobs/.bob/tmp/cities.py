import json,os,re,collections,sys
sys.path.insert(0,'.')
import workday_india_jobs as w
C=os.path.expanduser('~/.cache/mncs-jobs-scraper/')
recs=[json.loads(l) for l in open(C+'audit_probes.jsonl')]
ok=[r for r in recs if 'err' not in r]
ind=[r for r in ok if r['india']]; non=[r for r in ok if not r['india'] and r['total']]
# sanity: matcher on sites where india filter was found
hits_ind=sum(any(w.INDIA_LOCATION_RE.search(v[2]) for v in r['vals']) for r in ind)
print('sanity: India-filter sites where some facet value matches city regex:',hits_ind,'/',len(ind))
STATES=["Karnataka","Maharashtra","Tamil Nadu","Telangana","Andhra Pradesh","Haryana","Uttar Pradesh","West Bengal","Gujarat","Rajasthan","Kerala","Punjab","Madhya Pradesh","Odisha","Orissa","Bihar","Jharkhand","Chhattisgarh","Uttarakhand","Himachal","Assam","Goa","Delhi NCR","NCR","Jammu","Kashmir","Chandigarh","Maharastra","Telengana"]
MORE=["Bengaluru","Bangalore","BLR","Hyd","Secunderabad","Gachibowli","Hitec City","Madhapur","Kharadi","Hinjewadi","Magarpatta","Baner","Powai","Andheri","Bandra","Worli","Airoli","Vashi","Belapur","Sholinganallur","Taramani","Guindy","Siruseri","Perungudi","Manyata","Bellandur","Marathahalli","Koramangala","Indiranagar","Sarjapur","Cyber City","Cybercity","Okhla","Gurgaon","Kalyani","Rajarhat","Alwar","Sonipat","Rohtak","Amritsar","Ludhiana","Jalandhar","Srinagar","Jamshedpur","Raipur","Rourkela","Agra","Varanasi","Allahabad","Prayagraj","Meerut","Aurangabad","Kolhapur","Solapur","Pimpri","Hadapsar","Talegaon","Chakan","Ranjangaon","Sanand","Dahej","Bharuch","Rajkot","Jodhpur","Udaipur","Trichy","Tiruchirappalli","Tirunelveli","Vellore","Erode","Kozhikode","Calicut","Thrissur","Palakkad","Kakinada","Warangal","Nellore","Guntur","Kurnool","Anantapur","Davangere","Shimoga","Bellary","Tumkur","Hassan","Udupi","Bhilai","Cuttack","Siliguri","Durgapur","Asansol","Dhanbad","Silvassa","Daman","Panaji","Margao","Vasco","Lonavala","Mundra","Kandla","Haldia","Paradip","Vapi","Valsad","Ankleshwar","Pithampur","Dewas","Jabalpur","Gwalior","Bilaspur","Haridwar","Roorkee","Rudrapur","Baddi","Solan","Parwanoo","Nalagarh","Kullu","Shimla"]
rx=re.compile(r"(?<![A-Za-z])(?:"+"|".join(MORE+STATES)+r")(?![A-Za-z])",re.I)
codes=re.compile(r"(?<![A-Za-z])(IN|IND|INDIA)[-_ ]?[A-Z]{2,4}\b|\bIN-[A-Z]{2}\b|\b[A-Z]{2}-IN\b|\bIN\d{2}\b|\bIND\d{2}\b|\bINCO\b",)
cand=collections.defaultdict(list)
NOISE=re.compile(r"Indian(?:apolis|a)?\b|Indian[ -](?:Land|Trail|Wells|River|Rocks|Harbour|Head|Springs|Creek|Lake|School|Mound|Park|Town|Hills|Valley)|Winston|Agra,? OK|Bilaspur,? ?(?:WA)|Delhi,? (?:Ontario|NY|LA|IA)|Aurangabad",re.I)
for r in non:
    for p,l,d,c in r['vals']:
        m=rx.search(d) or w.INDIA_LOCATION_RE.search(d) or codes.search(d)
        if m and not NOISE.search(d):
            cand[r['name']].append((p,d,c,m.group(0)))
print('dropped sites with a plausible India facet value:',len(cand))
for n,v in sorted(cand.items(),key=lambda x:-len(x[1])): print(f"  {n[:34]:34}",[(x[3],x[1][:34],x[2]) for x in v[:4]])
