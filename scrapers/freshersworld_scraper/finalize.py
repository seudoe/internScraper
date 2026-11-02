"""
Finalize Freshersworld scrape results.
Deduplicates fw_raw.json and writes fw_internships.json with a quality report.
Run from the project root: python scrapers/freshersworld_scraper/finalize.py
"""
import json
import re
from pathlib import Path
from collections import Counter

DATA_DIR   = Path(__file__).resolve().parents[2] / "data" / "scrapers"
RAW_PATH   = DATA_DIR / "fw_raw.json"
FINAL_PATH = DATA_DIR / "fw_internships.json"

raw = json.load(open(RAW_PATH, encoding="utf-8"))

# Deduplicate by id
seen, unique = set(), []
for item in raw:
    key = item.get("id") or item.get("link", "")
    if key and key not in seen:
        seen.add(key)
        unique.append(item)

# Save final
with open(FINAL_PATH, "w", encoding="utf-8") as f:
    json.dump(unique, f, indent=2, ensure_ascii=False)

print(f"Total unique internships: {len(unique)}")

fields = ["company", "location", "salary", "experience", "qualifications", "posted"]
print("\n=== DATA COMPLETENESS ===")
for field in fields:
    pct = sum(1 for i in unique if i.get(field, "N/A") not in ("N/A", "")) / len(unique) * 100
    print(f"  {field:<18} {pct:.0f}%")

print("\n=== ROLE DISTRIBUTION ===")
for role, cnt in Counter(i.get("type", "Other") for i in unique).most_common():
    print(f"  {role:<35} {cnt}")

all_skills = [s for i in unique for s in i.get("skills", [])]
print("\n=== TOP SKILLS ===")
for skill, cnt in Counter(all_skills).most_common(10):
    print(f"  {skill:<20} {cnt}")

walkin = sum(1 for i in unique if i.get("is_walkin"))
hot    = sum(1 for i in unique if i.get("is_hot"))
print(f"\nWalk-in: {walkin} | Hot jobs: {hot}")
print(f"\nSaved to: {FINAL_PATH}")
