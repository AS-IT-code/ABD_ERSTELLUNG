"""
Zendesk -> Teams: ABD Erstellung PDF sync

Her gun calistir:
  python abd_sync.py

1) Subject icinde "ABD Erstellung" olan ticketlari bulur
2) Icinde dosya adinda "ABD" gecen PDF varsa indirir
3) PDF'i subject'ten turetilen isimle rename eder
4) Teams klasorune yukler
5) Bakilan ticket id'lerini Teams'teki processed_tickets.txt'ye yazar
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

SEARCH_TEXT = "ABD Erstellung"
PROCESSED_FILE = "processed_tickets.txt"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip().strip("'").strip('"')
    if not value:
        raise ValueError(f"Eksik env degeri: {name}")
    return value


def normalize_zendesk_subdomain(raw: str) -> str:
    """'aerosus' veya 'https://aerosus.zendesk.com/...' -> 'aerosus'."""
    value = raw.strip().rstrip("/")
    match = re.search(r"https?://([a-z0-9-]+)\.zendesk\.com", value, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    # yanlislikla path yapistirilmissa ilk parcayi al
    value = value.replace("https://", "").replace("http://", "")
    if ".zendesk.com" in value.lower():
        return value.split(".zendesk.com", 1)[0].split("/")[-1]
    return value.split("/")[0]


def zendesk_auth() -> tuple[str, tuple[str, str]]:
    subdomain = normalize_zendesk_subdomain(require_env("ZENDESK_SUBDOMAIN"))
    email = require_env("ZENDESK_EMAIL")
    token = require_env("ZENDESK_API_TOKEN")
    base = f"https://{subdomain}.zendesk.com/api/v2"
    auth = (f"{email}/token", token)
    print(f"Zendesk base: {base}")
    return base, auth


def get_teams_token() -> str:
    tenant = require_env("AZURE_TENANT_ID")
    client_id = require_env("Teams_ClientID")
    client_secret = require_env("Teams_SECRETKey")
    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=data, timeout=60)
    resp.raise_for_status()
    return resp.json()["access_token"]


def graph_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def drive_item_url(filename: str) -> str:
    drive_id = require_env("Teams_Drive_ID")
    folder_id = require_env("Teams_Folder_ID")
    # # ve bosluk URL'yi bozar; path segment encode edilmeli
    encoded = quote(filename, safe="")
    return f"{GRAPH_BASE}/drives/{drive_id}/items/{folder_id}:/{encoded}:"


def load_processed(token: str) -> set[str]:
    url = f"{drive_item_url(PROCESSED_FILE)}/content"
    resp = requests.get(url, headers=graph_headers(token), timeout=60)
    if resp.status_code == 404:
        return set()
    resp.raise_for_status()
    ids: set[str] = set()
    for line in resp.text.splitlines():
        line = line.strip()
        if line:
            ids.add(line)
    return ids


def save_processed(token: str, ticket_ids: set[str]) -> None:
    content = "\n".join(sorted(ticket_ids, key=lambda x: int(x) if x.isdigit() else x)) + "\n"
    url = f"{drive_item_url(PROCESSED_FILE)}/content"
    headers = {
        **graph_headers(token),
        "Content-Type": "text/plain",
    }
    resp = requests.put(url, headers=headers, data=content.encode("utf-8"), timeout=60)
    resp.raise_for_status()


def upload_file(token: str, local_path: Path, remote_name: str) -> None:
    url = f"{drive_item_url(remote_name)}/content"
    headers = {
        **graph_headers(token),
        "Content-Type": "application/pdf",
    }
    with local_path.open("rb") as f:
        resp = requests.put(url, headers=headers, data=f, timeout=300)
    resp.raise_for_status()


def sanitize_filename(name: str) -> str:
    name = name.strip()
    # Windows + SharePoint yasak karakterler (# ve % SharePoint'te de yasak)
    name = re.sub(r'[<>:"/\\|?*#%\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:180] or "ABD_Erstellung"


def pdf_name_from_subject(subject: str) -> str:
    subject = (subject or "").strip()
    # "AW: # 1007... ABD Erstellung" -> "# 1007... ABD Erstellung"
    match = re.search(r"(#\s*\d+.*)", subject, flags=re.IGNORECASE)
    if match:
        base = match.group(1).strip()
    else:
        base = re.sub(r"^(AW|RE|FW|WG)\s*:\s*", "", subject, flags=re.IGNORECASE).strip()
        if not base:
            base = subject
    if not base.lower().endswith(".pdf"):
        base = f"{base}.pdf"
    return sanitize_filename(base)


def search_tickets(base: str, auth: tuple[str, str]) -> list[dict]:
    tickets: list[dict] = []
    # subject icinde exact phrase
    query = f'type:ticket subject:"{SEARCH_TEXT}"'
    url = f"{base}/search.json"
    params = {"query": query, "sort_by": "created_at", "sort_order": "desc"}

    while url:
        resp = requests.get(url, auth=auth, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("results", []):
            if item.get("result_type") == "ticket" or "subject" in item:
                tickets.append(item)
        url = data.get("next_page")
        params = None  # next_page already includes params
    return tickets


def ticket_comments(base: str, auth: tuple[str, str], ticket_id: int | str) -> list[dict]:
    comments: list[dict] = []
    url = f"{base}/tickets/{ticket_id}/comments.json"
    while url:
        resp = requests.get(url, auth=auth, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        comments.extend(data.get("comments", []))
        url = data.get("next_page")
    return comments


def find_abd_pdfs(comments: list[dict]) -> list[dict]:
    found: list[dict] = []
    seen_urls: set[str] = set()
    for comment in comments:
        for att in comment.get("attachments", []) or []:
            filename = (att.get("file_name") or att.get("filename") or "").strip()
            content_url = att.get("content_url") or ""
            if not filename or not content_url:
                continue
            lower = filename.lower()
            if not lower.endswith(".pdf"):
                continue
            if "abd" not in lower:
                continue
            if content_url in seen_urls:
                continue
            seen_urls.add(content_url)
            found.append({"file_name": filename, "content_url": content_url})
    return found


def download_attachment(auth: tuple[str, str], content_url: str, dest: Path) -> None:
    resp = requests.get(content_url, auth=auth, timeout=120)
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def run_sync() -> dict:
    """Sync'i calistirir, ozet dict doner. Cloud Run / CLI ortak entry."""
    load_dotenv()
    zd_base, zd_auth = zendesk_auth()
    _ = require_env("Teams_Drive_ID")
    _ = require_env("Teams_Folder_ID")

    print("Teams token aliniyor...")
    token = get_teams_token()

    print(f"Islenmis ticket listesi ({PROCESSED_FILE}) okunuyor...")
    processed = load_processed(token)
    print(f"  -> {len(processed)} ticket daha once islenmis")

    print(f'Zendesk araniyor: subject icinde "{SEARCH_TEXT}"...')
    tickets = search_tickets(zd_base, zd_auth)
    print(f"  -> {len(tickets)} ticket bulundu")

    uploaded = 0
    skipped = 0
    no_pdf = 0
    errors = 0
    newly_processed: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="abd_sync_") as tmp:
        tmp_dir = Path(tmp)

        for ticket in tickets:
            ticket_id = str(ticket.get("id", "")).strip()
            subject = (ticket.get("subject") or "").strip()
            if not ticket_id:
                continue

            if ticket_id in processed:
                skipped += 1
                continue

            print(f"\nTicket {ticket_id}: {subject}")
            try:
                comments = ticket_comments(zd_base, zd_auth, ticket_id)
                pdfs = find_abd_pdfs(comments)

                if not pdfs:
                    print("  PDF (ABD) yok -> islenmis sayilacak")
                    newly_processed.add(ticket_id)
                    processed.add(ticket_id)
                    no_pdf += 1
                    continue

                remote_name = pdf_name_from_subject(subject)
                for idx, pdf in enumerate(pdfs):
                    name = remote_name
                    if idx > 0:
                        stem = Path(remote_name).stem
                        name = sanitize_filename(f"{stem}_{idx + 1}.pdf")

                    local_path = tmp_dir / name
                    print(f"  Indiriliyor: {pdf['file_name']} -> {name}")
                    download_attachment(zd_auth, pdf["content_url"], local_path)
                    print(f"  Teams'e yukleniyor: {name}")
                    upload_file(token, local_path, name)
                    uploaded += 1

                newly_processed.add(ticket_id)
                processed.add(ticket_id)
            except Exception as exc:  # noqa: BLE001 - tek ticket hata verse devam et
                errors += 1
                print(f"  HATA (ticket atlandi): {exc}")
                continue

            if len(newly_processed) % 10 == 0:
                save_processed(token, processed)
                print(f"  [checkpoint] processed_tickets.txt kaydedildi ({len(processed)} total)")

    if newly_processed:
        print(f"\n{PROCESSED_FILE} guncelleniyor ({len(newly_processed)} yeni)...")
        save_processed(token, processed)
    else:
        print("\nYeni islenen ticket yok, processed listesi ayni.")

    summary = {
        "uploaded": uploaded,
        "skipped": skipped,
        "no_pdf": no_pdf,
        "errors": errors,
        "newly_processed": len(newly_processed),
        "tickets_found": len(tickets),
    }
    print("\n--- Ozet ---")
    for key, value in summary.items():
        print(f"{key}: {value}")
    return summary


def main() -> int:
    result = run_sync()
    return 0 if result.get("errors", 0) == 0 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.HTTPError as exc:
        print(f"HTTP hata: {exc.response.status_code} {exc.response.text[:500]}", file=sys.stderr)
        raise SystemExit(1)
    except requests.ConnectionError as exc:
        print(f"Baglanti hatasi: {exc}", file=sys.stderr)
        print(
            "Ipucu: ZENDESK_SUBDOMAIN sadece subdomain olmali, ornek: aerosus "
            "(tam URL degil).",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Iptal edildi.", file=sys.stderr)
        raise SystemExit(130)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
