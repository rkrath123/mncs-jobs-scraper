
# =====================================================
# MNCS JOBS SCRAPER - GOOGLE COLAB
# =====================================================

import os
import subprocess
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from IPython.display import IFrame, display
from google.colab import files

# 1. Repository details
REPO_URL = "https://github.com/rkrath123/mncs-jobs-scraper.git"
REPO_DIR = Path("/content/mncs-jobs-scraper")

# 2. Get today's date in India timezone
TODAY = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")

print(f"Execution date: {TODAY}")

# 3. Clone repository
if not REPO_DIR.exists():
    subprocess.run(
        ["git", "clone", REPO_URL, str(REPO_DIR)],
        check=True
    )
else:
    print("Repository already exists. Using existing clone.")

os.chdir(REPO_DIR)

print("\nRepository ready!")

# 4. Check Python version
subprocess.run(["python3", "--version"], check=True)

# 5. Run scraper
print("\nStarting job scraper...\n")

subprocess.run(
    ["python3", "fintech_scrape_jobs.py", "--show-all"],
    check=True
)

# 6. Locate today's report
REPORT = REPO_DIR / "output" / TODAY / "report.html"

if not REPORT.exists():
    raise FileNotFoundError(
        f"Today's report not found: {REPORT}"
    )

print("\n===================================")
print("SCRAPING COMPLETED SUCCESSFULLY")
print("===================================")
print(f"Report location: {REPORT}")

# 7. Display report in Google Colab
print("\nDisplaying HTML report...")

display(IFrame(
    src=str(REPORT),
    width="100%",
    height=900
))

# 8. Download report to local computer
print("\nDownloading report.html...")

files.download(str(REPORT))

print("\nAll tasks completed!")
