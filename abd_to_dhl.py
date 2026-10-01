"""
Akis 2 (ayri Cloud Scheduler):
  ABD Erstellung -> commercial + ShippingLabel + ABD PDF
  -> zip -> Teams ABDtoDHL
  -> Zendesk ticket (public reply + zip'ler), status solved

Gunluk batch (bir veya birden fazla zip tek ticket'ta):
  python abd_to_dhl.py

Tek kaynak ticket:
  python abd_to_dhl.py 430730
  python abd_to_dhl.py 430730 --force
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv

from abd_sync import (
    SEARCH_TEXT,
    download_attachment,
    get_search_window,
    get_teams_token,
    graph_headers,
    require_env,
    sanitize_filename,
    search_tickets,
    ticket_comments,
    zendesk_auth,
)

PROCESSED_DHL_FILE = "processed_abdtodhl.txt"

# Birden fazla zip -> cogul sablon
MAIL_SUBJECT_MULTI = (
    "Ausfuhrbegleitdokumente für die Non-EU Sendungen. AT Parts Germany GmbH"
)
MAIL_BODY_MULTI = """Sehr geehrte Damen und Herren,

im Anhang finden Sie die Ausfuhrbegleitdokumente für die Non-EU-Sendungen.

Wir bitten Sie, die entsprechenden weiteren Schritte in die Wege zu leiten.

Vielen Dank für Ihre Unterstützung.

Mit freundlichen Grüßen

Asya Yildirim
AT Parts Germany GmbH"""


def mail_subject_single(zip_stem: str) -> str:
    return (
        f"Ausfuhrbegleitdokument für die Non-EU Sendung {zip_stem}. "
        "AT Parts Germany GmbH"
    )


def mail_body_single(zip_stem: str) -> str:
    return f"""Sehr geehrte Damen und Herren,

im Anhang finden Sie das Ausfuhrbegleitdokument für die Non-EU-Sendung {zip_stem}.

Wir bitten Sie, die entsprechenden weiteren Schritte in die Wege zu leiten.

Vielen Dank für Ihre Unterstützung.

Mit freundlichen Grüßen

Asya Yildirim
AT Parts Germany GmbH"""


def env(name: str, *alts: str, required: bool = True) -> str:
    for key in (name, *alts):
        value = os.getenv(key, "").strip().strip("'").strip('"')
        if value:
            return value
    if required:
        raise ValueError(f"Eksik env: {name}")
    return ""


def abdtodhl_folder_id() -> str:
    return env("Teams_ABDtoDHL_Folder_ID", "ABDtoDHL")


def dhl_since() -> str:
    return env("DHL_SINCE", required=False) or "2026-09-29"


def drive_item_url_in_folder(folder_id: str, filename: str) -> str:
    from urllib.parse import quote

    from abd_sync import GRAPH_BASE

    drive_id = require_env("Teams_Drive_ID")
    encoded = quote(filename, safe="")
    return f"{GRAPH_BASE}/drives/{drive_id}/items/{folder_id}:/{encoded}:"


def upload_to_folder(token: str, folder_id: str, local_path: Path, remote_name: str) -> None:
    url = f"{drive_item_url_in_folder(folder_id, remote_name)}/content"
    ctype = "application/zip" if remote_name.lower().endswith(".zip") else "application/octet-stream"
    headers = {**graph_headers(token), "Content-Type": ctype}
    with local_path.open("rb") as f:
        resp = requests.put(url, headers=headers, data=f, timeout=300)
    resp.raise_for_status()


def load_processed_dhl(token: str, folder_id: str) -> set[str]:
    url = f"{drive_item_url_in_folder(folder_id, PROCESSED_DHL_FILE)}/content"
    resp = requests.get(url, headers=graph_headers(token), timeout=60)
    if resp.status_code == 404:
        return set()
    resp.raise_for_status()
    return {line.strip() for line in resp.text.splitlines() if line.strip()}


def save_processed_dhl(token: str, folder_id: str, ticket_ids: set[str]) -> None:
    content = "\n".join(sorted(ticket_ids, key=lambda x: int(x) if x.isdigit() else x)) + "\n"
    url = f"{drive_item_url_in_folder(folder_id, PROCESSED_DHL_FILE)}/content"
    headers = {**graph_headers(token), "Content-Type": "text/plain"}
    resp = requests.put(url, headers=headers, data=content.encode("utf-8"), timeout=60)
    resp.raise_for_status()


def classify_pdfs(comments: list[dict]) -> dict:
    result = {
        "commercial": None,
        "shipping": None,
        "abd": None,
        "shipping_number": None,
    }

    for comment in comments:
        body = (comment.get("body") or "").strip()
        for att in comment.get("attachments") or []:
            filename = (att.get("file_name") or "").strip()
            content_url = att.get("content_url") or ""
            if not filename.lower().endswith(".pdf") or not content_url:
                continue
            lower = filename.lower()
            item = {"file_name": filename, "content_url": content_url, "comment_body": body}

            if "shippinglabel" in lower or "shipping_label" in lower or "shipping label" in lower:
                result["shipping"] = item
                nums = re.findall(r"\b(\d{6,})\b", body)
                if nums:
                    result["shipping_number"] = nums[0]
                else:
                    m = re.search(r"_(\d{6,})\)?\.pdf$", filename, flags=re.IGNORECASE)
                    if m:
                        result["shipping_number"] = m.group(1)
            elif "commercial" in lower or "commerical" in lower or "invoice" in lower:
                if "abd" not in lower:
                    result["commercial"] = item
            elif "abd" in lower:
                result["abd"] = item

    return result


def upload_zip_to_zendesk(base: str, auth: tuple[str, str], zip_path: Path) -> str:
    url = f"{base}/uploads.json"
    params = {"filename": zip_path.name}
    headers = {"Content-Type": "application/binary"}
    with zip_path.open("rb") as f:
        resp = requests.post(url, auth=auth, params=params, headers=headers, data=f, timeout=300)
    resp.raise_for_status()
    return resp.json()["upload"]["token"]


def zip_stem(zip_path: Path) -> str:
    name = zip_path.name
    if name.lower().endswith(".zip"):
        return name[:-4]
    return zip_path.stem


def build_mail_copy(zip_paths: list[Path]) -> tuple[str, str]:
    """Tek zip -> tekil sablon (+ dosya adi); birden fazla -> cogul sablon."""
    if len(zip_paths) == 1:
        stem = zip_stem(zip_paths[0])
        return mail_subject_single(stem), mail_body_single(stem)
    return MAIL_SUBJECT_MULTI, MAIL_BODY_MULTI


def create_dhl_ticket(base: str, auth: tuple[str, str], zip_paths: list[Path]) -> dict:
    """Yeni Zendesk ticket: public reply + zip, status solved."""
    if not zip_paths:
        raise ValueError("Zip listesi bos")

    subject, body = build_mail_copy(zip_paths)
    upload_tokens = [upload_zip_to_zendesk(base, auth, p) for p in zip_paths]

    mailto = env("mailtobesent", "MAIL_TO_BE_SENT")
    agent_id = env("AGENT_ID", required=False)
    agent_email = env("AGENT_EMAIL", required=False)
    agent_name = env("AGENT_NAME", required=False) or "Asya Yildirim"
    group_id = env("GroupID", "GROUP_ID", required=False)
    tag = env("ticket_tag", "TICKET_TAG", required=False)
    type_field_id = env("CUSTOM_TICKET_TYPE_FIELD_ID", required=False)
    type_field_value = env("CUSTOM_TICKET_TYPE_FIELD_VALUE", required=False)
    # Solved zorunlu text field'lar (Cloud Run'da /'li env okunmayabilir -> sabit ID fallback)
    na_id = (
        env("CUSTOM_NA_ID", "custom_N/A_id", required=False) or "24929440954012"
    )
    na_id2 = (
        env("CUSTOM_NA_ID2", "custom_N/A_id2", required=False) or "16374006240284"
    )
    na_value = env("CUSTOM_NA_VALUE", required=False) or "N/A"

    ticket: dict = {
        "subject": subject,
        "status": "solved",
        "requester": {"email": mailto, "name": mailto.split("@")[0]},
        "comment": {
            "body": body,
            "public": True,
            "uploads": upload_tokens,
        },
    }

    if agent_id and agent_id.isdigit():
        aid = int(agent_id)
        ticket["assignee_id"] = aid
        ticket["submitter_id"] = aid  # ticket'i agent acmis gibi
        ticket["comment"]["author_id"] = aid  # public reply gonderen = AGENT
    if group_id and group_id.isdigit():
        ticket["group_id"] = int(group_id)
    if tag:
        ticket["tags"] = [t.strip() for t in tag.split(",") if t.strip()]

    custom_fields = []
    if type_field_id and type_field_value and type_field_id.isdigit():
        custom_fields.append({"id": int(type_field_id), "value": type_field_value})
    # Solved icin her zaman N/A yaz (eksikse 422)
    if na_id.isdigit():
        custom_fields.append({"id": int(na_id), "value": na_value})
    if na_id2.isdigit():
        custom_fields.append({"id": int(na_id2), "value": na_value})
    ticket["custom_fields"] = custom_fields
    print(f"  Solved custom fields: {custom_fields}")

    resp = requests.post(f"{base}/tickets.json", auth=auth, json={"ticket": ticket}, timeout=180)
    if resp.status_code >= 400:
        raise RuntimeError(f"Zendesk ticket create {resp.status_code}: {resp.text[:800]}")
    created = resp.json().get("ticket") or {}
    mode = "tekil" if len(zip_paths) == 1 else f"toplu({len(zip_paths)})"
    print(
        f"  Zendesk ticket acildi+solved: #{created.get('id')} [{mode}] "
        f"(public reply, assignee={agent_name}/{agent_email}, group={group_id}, tag={tag})"
    )
    print(f"  Subject: {subject}")
    return created


def ticket_created_on_or_after(ticket: dict, since_yyyy_mm_dd: str) -> bool:
    created = (ticket.get("created_at") or "")[:10]
    if not created:
        return True
    return created >= since_yyyy_mm_dd


def build_zip_from_classified(
    zd_auth: tuple[str, str],
    classified: dict,
    tmp_dir: Path,
) -> tuple[Path, str]:
    ship_no = classified["shipping_number"]
    if not ship_no:
        raise ValueError("ShippingLabel comment numarasi bulunamadi")
    zip_name = sanitize_filename(f"{ship_no}.zip")
    zip_path = tmp_dir / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key in ("commercial", "shipping", "abd"):
            pdf = classified[key]
            local = tmp_dir / pdf["file_name"]
            print(f"  Indiriliyor: {pdf['file_name']}")
            download_attachment(zd_auth, pdf["content_url"], local)
            zf.write(local, arcname=pdf["file_name"])
    return zip_path, zip_name


def prepare_zip_for_ticket(
    ticket_id: str,
    *,
    force: bool = False,
    comments: list[dict] | None = None,
    ticket_meta: dict | None = None,
    out_dir: Path,
    zd_base: str | None = None,
    zd_auth: tuple[str, str] | None = None,
    folder_id: str | None = None,
    token: str | None = None,
    processed: set[str] | None = None,
) -> dict:
    """Zip olustur + Teams'e yukle. Zendesk ticket acmaz (batch sonunda toplu acilir)."""
    load_dotenv()
    if zd_base is None or zd_auth is None:
        zd_base, zd_auth = zendesk_auth()
    if folder_id is None:
        folder_id = abdtodhl_folder_id()
    if token is None:
        token = get_teams_token()
    if processed is None:
        processed = load_processed_dhl(token, folder_id)
    since = dhl_since()

    if not force and ticket_id in processed:
        return {"ok": True, "skipped": True, "ticket_id": ticket_id, "reason": "already_processed"}

    if ticket_meta and not force and not ticket_created_on_or_after(ticket_meta, since):
        return {
            "ok": True,
            "skipped": True,
            "ticket_id": ticket_id,
            "reason": f"created_before_{since}",
        }

    if comments is None:
        comments = ticket_comments(zd_base, zd_auth, ticket_id)
    classified = classify_pdfs(comments)

    missing = [k for k in ("commercial", "shipping", "abd") if not classified[k]]
    if missing:
        return {
            "ok": False,
            "waiting": True,
            "ticket_id": ticket_id,
            "error": f"Eksik PDF: {', '.join(missing)}",
        }

    work = out_dir / f"t_{ticket_id}"
    work.mkdir(parents=True, exist_ok=True)
    zip_path, zip_name = build_zip_from_classified(zd_auth, classified, work)

    print(f"  Zip: {zip_name}")
    print("  Teams ABDtoDHL'e yukleniyor...")
    upload_to_folder(token, folder_id, zip_path, zip_name)

    # Batch temp kokune kopyala (tek isim)
    final_path = out_dir / zip_name
    if zip_path.resolve() != final_path.resolve():
        shutil.copy2(zip_path, final_path)

    return {
        "ok": True,
        "ticket_id": ticket_id,
        "zip_name": zip_name,
        "zip_path": str(final_path),
        "shipping_number": classified["shipping_number"],
    }


def process_ticket(ticket_id: str, *, force: bool = False) -> dict:
    """Tek ticket: zip + Teams + Zendesk (tekil sablon)."""
    load_dotenv()
    zd_base, zd_auth = zendesk_auth()
    folder_id = abdtodhl_folder_id()
    token = get_teams_token()
    processed = load_processed_dhl(token, folder_id)

    with tempfile.TemporaryDirectory(prefix="abdtodhl_") as tmp:
        result = prepare_zip_for_ticket(
            ticket_id,
            force=force,
            out_dir=Path(tmp),
            zd_base=zd_base,
            zd_auth=zd_auth,
            folder_id=folder_id,
            token=token,
            processed=processed,
        )
        if not result.get("ok") or result.get("skipped") or result.get("waiting"):
            return result

        zip_path = Path(result["zip_path"])
        print("  Zendesk ticket (public reply + zip, solved) aciliyor...")
        created = create_dhl_ticket(zd_base, zd_auth, [zip_path])

        processed.add(ticket_id)
        save_processed_dhl(token, folder_id, processed)
        result["zendesk_ticket_id"] = created.get("id")
        return result


def run_dhl_batch() -> dict:
    """
    Gunluk Cloud Scheduler girisi:
    - Kaynak ticketlari ara
    - Hazir olanlardan zip uret / Teams'e at
    - 1 zip -> tekil mail sablonu
    - N zip -> cogul mail sablonu (tek Zendesk ticket, tum zipler ekli)
    """
    load_dotenv()
    since = dhl_since()
    folder_id = abdtodhl_folder_id()
    token = get_teams_token()
    zd_base, zd_auth = zendesk_auth()
    processed = load_processed_dhl(token, folder_id)
    window = get_search_window()

    print(f"=== Akis 2 (ayri): ABDtoDHL batch (DHL_SINCE={since}) ===")
    print(f"  DHL processed: {len(processed)}")
    print(f'  Zendesk araniyor: subject "{SEARCH_TEXT}" + {window}...')
    tickets = search_tickets(zd_base, zd_auth, window)
    print(f"  -> {len(tickets)} ticket")

    uploaded = 0
    skipped = 0
    waiting = 0
    errors = 0
    ready_source_ids: list[str] = []
    zip_paths: list[Path] = []

    with tempfile.TemporaryDirectory(prefix="abdtodhl_batch_") as tmp:
        out_dir = Path(tmp)

        for ticket in tickets:
            ticket_id = str(ticket.get("id", "")).strip()
            if not ticket_id:
                continue
            if ticket_id in processed:
                skipped += 1
                continue
            if not ticket_created_on_or_after(ticket, since):
                skipped += 1
                continue

            subject = (ticket.get("subject") or "").strip()
            print(f"\n[DHL] Ticket {ticket_id}: {subject}")
            try:
                comments = ticket_comments(zd_base, zd_auth, ticket_id)
                result = prepare_zip_for_ticket(
                    ticket_id,
                    force=False,
                    comments=comments,
                    ticket_meta=ticket,
                    out_dir=out_dir,
                    zd_base=zd_base,
                    zd_auth=zd_auth,
                    folder_id=folder_id,
                    token=token,
                    processed=processed,
                )
                if result.get("skipped"):
                    skipped += 1
                elif result.get("waiting"):
                    waiting += 1
                    print(f"  Bekleniyor: {result.get('error')}")
                elif result.get("ok"):
                    uploaded += 1
                    ready_source_ids.append(ticket_id)
                    zip_paths.append(Path(result["zip_path"]))
                else:
                    errors += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"  HATA (DHL): {exc}")

        zendesk_ticket_id = None
        if zip_paths:
            print(f"\n  Zendesk ticket (public reply + solved) aciliyor ({len(zip_paths)} zip)...")
            created = create_dhl_ticket(zd_base, zd_auth, zip_paths)
            zendesk_ticket_id = created.get("id")
            for tid in ready_source_ids:
                processed.add(tid)
            save_processed_dhl(token, folder_id, processed)
        else:
            print("\n  Gonderilecek zip yok; Zendesk ticket acilmadi.")

    summary = {
        "dhl_zip_uploaded": uploaded,
        "dhl_skipped": skipped,
        "dhl_waiting": waiting,
        "dhl_errors": errors,
        "dhl_zendesk_ticket_id": zendesk_ticket_id,
        "dhl_zip_count": len(ready_source_ids),
        "dhl_since": since,
        "mail_mode": (
            "single" if len(ready_source_ids) == 1 else ("multi" if ready_source_ids else "none")
        ),
    }
    print("\n--- DHL Ozet ---")
    for k, v in summary.items():
        print(f"{k}: {v}")
    return summary


def main() -> int:
    load_dotenv()
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv

    if not args:
        # Cloud Scheduler: python abd_to_dhl.py
        result = run_dhl_batch()
        return 0 if result.get("dhl_errors", 0) == 0 else 2

    ticket_id = args[0].strip()
    print(f"ABDtoDHL tek ticket: {ticket_id} (force={force})")
    result = process_ticket(ticket_id, force=force)
    print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
