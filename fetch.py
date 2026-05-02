import asyncio
from playwright.async_api import async_playwright
import csv
import json
import os
import re
from datetime import datetime
from html import escape
from urllib.parse import quote

BASE_DOMAIN = "https://citrus.realforeclose.com/"
CALENDAR_URL = f"{BASE_DOMAIN}/index.cfm?zaction=USER&zmethod=CALENDAR"

DATA_DIR = "data"
DOCS_DIR = "docs"
TODAY_STR = datetime.now().strftime("%Y-%m-%d")
TODAY_FILE = os.path.join(DATA_DIR, f"{TODAY_STR}.csv")
SEEN_FILE = os.path.join(DATA_DIR, "all_seen.csv")
INDEX_FILE = os.path.join(DATA_DIR, "index.json")
HTML_FILE = os.path.join(DOCS_DIR, "index.html")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DOCS_DIR, exist_ok=True)


def clean_text(value: str) -> str:
    return " ".join(str(value).replace("\xa0", " ").split())


def load_seen() -> set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()

    seen = set()
    with open(SEEN_FILE, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[0].strip():
                seen.add(row[0].strip())
    return seen


def save_seen(seen: set[str]) -> None:
    with open(SEEN_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for case_no in sorted(seen):
            writer.writerow([case_no])


def update_index() -> list[str]:
    files = sorted(
        [f for f in os.listdir(DATA_DIR) if f.endswith(".csv") and f != "all_seen.csv"],
        reverse=True,
    )
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(files, f)
    return files


def extract_auctions_waiting(text: str) -> str:
    start = text.find("Auctions Waiting")
    if start == -1:
        return ""

    section = text[start:]

    stop_markers = [
        "Auctions Closed",
        "Closed Auctions",
        "Canceled Auctions",
        "Auctions Canceled",
        "Sales List",
        "Connection",
        "About Us | Site Map |",
    ]

    end_positions = [section.find(marker) for marker in stop_markers if section.find(marker) != -1]
    if end_positions:
        section = section[:min(end_positions)]

    return section


def clean_money_field(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    if re.search(r"\bHidden\b", value, flags=re.IGNORECASE):
        return "Hidden"
    money = re.search(r"\$\s*\d[\d,]*\.\d{2}", value)
    return money.group(0).replace("$ ", "$") if money else ""


def clean_case_field(value: str) -> str:
    value = clean_text(value)
    m = re.search(r"\b\d{4}\s+[A-Z]{1,3}\s+\d{3,8}\s*[A-Z]?\b", value, flags=re.IGNORECASE)
    return clean_text(m.group(0)).upper() if m else value.upper()


def clean_parcel_field(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"\bProperty Appraiser\b.*$", "", value, flags=re.IGNORECASE).strip()
    if re.search(r"multiple\s+parcels", value, flags=re.IGNORECASE):
        return "MULTIPLE PARCELS"
    m = re.search(r"\b\d{5,}\b", value)
    return m.group(0) if m else ""


def clean_address_field(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"\bProperty Appraiser\b.*$", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"\bAssessed Value:.*$", "", value, flags=re.IGNORECASE).strip()
    return value


def get_between(text: str, start_label: str, end_labels: list[str]) -> str:
    """Get everything after start_label until the first later label."""
    start_re = re.search(re.escape(start_label), text, flags=re.IGNORECASE)
    if not start_re:
        return ""
    start = start_re.end()
    end = len(text)
    for label in end_labels:
        m = re.search(re.escape(label), text[start:], flags=re.IGNORECASE)
        if m:
            end = min(end, start + m.start())
    return clean_text(text[start:end])


def parse_block(block: str) -> dict | None:
    auction_match = re.search(
        r"Auction Starts\s*(?P<auction_date>\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}\s+[AP]M\s+ET)",
        block,
        re.IGNORECASE,
    )
    if not auction_match or "Case #:" not in block:
        return None

    case_no = clean_case_field(get_between(block, "Case #:", ["Final Judgment Amount:", "Parcel ID:", "Property Address:", "Assessed Value:", "Plaintiff Max Bid:", "Auction Starts"]))
    final_judgment = clean_money_field(get_between(block, "Final Judgment Amount:", ["Parcel ID:", "Property Address:", "Assessed Value:", "Plaintiff Max Bid:", "Auction Starts"]))
    parcel_id = clean_parcel_field(get_between(block, "Parcel ID:", ["Property Address:", "Assessed Value:", "Plaintiff Max Bid:", "Auction Starts"]))
    address = clean_address_field(get_between(block, "Property Address:", ["Assessed Value:", "Plaintiff Max Bid:", "Auction Starts"]))
    assessed_value = clean_money_field(get_between(block, "Assessed Value:", ["Plaintiff Max Bid:", "Auction Starts"]))
    max_bid = clean_money_field(get_between(block, "Plaintiff Max Bid:", ["Auction Starts"]))

    if not case_no:
        return None

    return {
        "Auction Date": clean_text(auction_match.group("auction_date")),
        "Property Address": address,
        "Final Judgment": final_judgment,
        "Assessed Value": assessed_value,
        "Plaintiff Max Bid": max_bid,
        "Case #": case_no,
        "Parcel ID": parcel_id,
        "Case Link": f"{BASE_DOMAIN}/index.cfm?zaction=auction&zmethod=details&AID={quote(case_no, safe='')}&bypassPage=1",
        "Parcel Link": f"https://pcpao.gov/Parcel-Details/{quote(parcel_id, safe='')}" if parcel_id and parcel_id.upper() != "MULTIPLE PARCELS" else "",
    }


def parse_waiting_records(section_text: str) -> list[dict]:
    if not section_text:
        return []

    # Split each auction card by the next Auction Starts line.
    blocks = re.split(
        r"(?=Auction Starts\s*\d{2}/\d{2}/\d{4}\s+\d{1,2}:\d{2}\s+[AP]M\s+ET)",
        section_text,
        flags=re.IGNORECASE,
    )

    rows = []
    for block in blocks:
        row = parse_block(block.strip())
        if row:
            rows.append(row)

    return rows


def write_daily(rows: list[dict]) -> None:
    with open(TODAY_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Auction Date",
            "Property Address",
            "Final Judgment",
            "Assessed Value",
            "Plaintiff Max Bid",
            "Case #",
            "Parcel ID",
            "Case Link",
            "Parcel Link",
        ])
        for r in rows:
            writer.writerow([
                r["Auction Date"],
                r["Property Address"],
                r["Final Judgment"],
                r["Assessed Value"],
                r["Plaintiff Max Bid"],
                r["Case #"],
                r["Parcel ID"],
                r["Case Link"],
                r["Parcel Link"],
            ])


def read_csv_rows(path: str) -> list[dict]:
    rows = []
    if not os.path.exists(path):
        return rows

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = dict(row)
            row["Final Judgment"] = clean_money_field(row.get("Final Judgment", ""))
            row["Assessed Value"] = clean_money_field(row.get("Assessed Value", ""))
            row["Plaintiff Max Bid"] = clean_money_field(row.get("Plaintiff Max Bid", ""))
            row["Parcel ID"] = clean_parcel_field(row.get("Parcel ID", "")) or row.get("Parcel ID", "")
            row["Case #"] = clean_case_field(row.get("Case #", ""))
            rows.append(row)
    return rows


def build_html(index_files: list[str]) -> None:
    list_items = []
    sections = []

    for i, file in enumerate(index_files):
        date_label = file.replace(".csv", "")
        section_id = f"day-{date_label}"
        active_class = "active" if i == 0 else ""

        list_items.append(
            f'<li><a href="#{section_id}" class="{active_class}">{escape(date_label)}</a></li>'
        )

        rows = read_csv_rows(os.path.join(DATA_DIR, file))
        if rows:
            body_rows = "\n".join(
                f"""
                <tr>
                  <td>{escape(r.get("Auction Date", ""))}</td>
                  <td>{escape(r.get("Property Address", ""))}</td>
                  <td>{escape(r.get("Final Judgment", ""))}</td>
                  <td>{escape(r.get("Assessed Value", ""))}</td>
                  <td>{escape(r.get("Plaintiff Max Bid", ""))}</td>
                  <td><a href="{escape(r.get("Case Link", ""))}" target="_blank">{escape(r.get("Case #", ""))}</a></td>
                  <td><a href="{escape(r.get("Parcel Link", ""))}" target="_blank">{escape(r.get("Parcel ID", ""))}</a></td>
                </tr>
                """
                for r in rows
            )
        else:
            body_rows = '<tr><td colspan="7">No records</td></tr>'

        sections.append(
            f"""
            <section id="{section_id}" class="day-section">
              <h2>{escape(date_label)}</h2>
              <table>
                <thead>
                  <tr>
                    <th>Auction Date</th>
                    <th>Property Address</th>
                    <th>Final Judgment</th>
                    <th>Assessed Value</th>
                    <th>Plaintiff Max Bid</th>
                    <th>Case #</th>
                    <th>Parcel ID</th>
                  </tr>
                </thead>
                <tbody>
                  {body_rows}
                </tbody>
              </table>
            </section>
            """
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Daily New Foreclosures</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 20px; }}
    h1 {{ margin-bottom: 16px; }}
    ul {{ padding-left: 20px; }}
    li {{ margin-bottom: 6px; }}
    a {{ color: #0645ad; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 10px; margin-bottom: 28px; }}
    th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f3f3; }}
  </style>
</head>
<body>
  <h1>Daily New Foreclosures</h1>
  <ul>
    {''.join(list_items) if list_items else '<li>No data files yet</li>'}
  </ul>
  {''.join(sections) if sections else '<p>No data files yet.</p>'}
</body>
</html>
"""
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)


async def get_month_info(page) -> tuple[list[dict], str | None]:
    boxes = await page.locator(".CALBOX").all()
    days = []

    for idx in range(len(boxes)):
        try:
            box = page.locator(".CALBOX").nth(idx)
            text = clean_text(await box.inner_text(timeout=3000))
        except Exception:
            continue

        if "Foreclosure" not in text or "FC" not in text:
            continue

        m = re.search(r"^(\d+).*?(\d+)\s*/\s*(\d+)\s*FC", text)
        if not m:
            continue

        day_num = int(m.group(1))
        active = int(m.group(2))
        scheduled = int(m.group(3))

        if active <= 0:
            continue

        days.append({
            "index": idx,
            "day": day_num,
            "active": active,
            "scheduled": scheduled,
        })

    next_month_url = None
    links = await page.locator("a").evaluate_all(
        """
        els => els.map(a => ({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.href || ''
        }))
        """
    )
    candidates = []
    for link in links:
        href = link["href"]
        if "zmethod=calendar" in href.lower() and "selCalDate=" in href:
            candidates.append(href)

    if candidates:
        next_month_url = candidates[-1]

    return days, next_month_url


async def scrape():
    seen_cases = load_seen()
    all_rows_for_today = []
    visited_months = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        current_month_url = CALENDAR_URL
        empty_month_streak = 0

        while current_month_url and current_month_url not in visited_months:
            visited_months.add(current_month_url)

            await page.goto(current_month_url, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)

            days, next_month_url = await get_month_info(page)
            print(f"Found {len(days)} live foreclosure days in {current_month_url}")

            if not days:
                empty_month_streak += 1
                if empty_month_streak >= 1:
                    break
            else:
                empty_month_streak = 0

            for item in days:
                try:
                    print(f"Opening day {item['day']} with {item['active']} live auctions")

                    await page.goto(current_month_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)

                    await page.locator(".CALBOX").nth(item["index"]).click(timeout=5000, force=True)
                    await page.wait_for_timeout(5000)

                    body_text = await page.locator("body").inner_text()
                    waiting_text = extract_auctions_waiting(body_text)
                    rows = parse_waiting_records(waiting_text)

                    print(f"  Parsed {len(rows)} waiting records")

                    for r in rows:
                        case_no = r["Case #"]
                        if not case_no:
                            continue
                        all_rows_for_today.append(r)
                        seen_cases.add(case_no)

                except Exception as e:
                    print(f"skip day {item['day']} error: {e}")
                    continue

            current_month_url = next_month_url

        await browser.close()

    write_daily(all_rows_for_today)
    save_seen(seen_cases)
    index_files = update_index()
    build_html(index_files)
    print(f"Saved {len(all_rows_for_today)} records for today")


if __name__ == "__main__":
    asyncio.run(scrape())