from __future__ import annotations

import os
import shutil
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import piexif
import piexif.helper

# Use the SAME client and image helpers as the CLI/Docker
from famly_fetch.api_client import ApiClient
from famly_fetch.image import BaseImage, Image as PublicImage, SecretImage


# ---------- Filename helpers ----------
_INVALID = set('<>:"/\\|?*')
def _safe_name(s: str) -> str:
    clean = "".join("_" if ch in _INVALID else ch for ch in (s or ""))
    return clean.strip().rstrip(".")


# ---------- Session wrapper ----------
@dataclass
class Session:
    client: ApiClient


def login_and_get_session(email: str, password: str) -> Session:
    """
    Log in exactly like the CLI/Docker does.
    Optional override for DE tenants: set FAMLY_APP_BASE=https://app.famly.de
    """
    c = ApiClient()
    base_override = os.environ.get("FAMLY_APP_BASE")
    if base_override:
        c._base = base_override.rstrip("/")
    c.login(email, password)
    return Session(client=c)


# ---------- Children discovery (matches downloader.get_all_children) ----------
def list_children_best_effort(session: Session) -> List[Dict[str, str]]:
    """
    Returns: [{"id": <childId>, "label": <display name>}, ...]
    """
    me = session.client.me_me_me() or {}
    kids: List[Dict[str, str]] = []

    # current children from roles2
    for role in me.get("roles2", []):
        kids.append({"id": role.get("targetId"), "label": role.get("title")})

    # previous children via behaviors[ShowPreviousChildren]
    for ele in me.get("behaviors", []):
        if ele.get("id") == "ShowPreviousChildren":
            for ch in ele.get("payload", {}).get("children", []):
                kids.append({
                    "id": ch.get("childId"),
                    "label": (ch.get("name") or {}).get("firstName")
                })

    # de-dupe
    seen, uniq = set(), []
    for k in kids:
        cid = k.get("id")
        if cid and cid not in seen:
            seen.add(cid)
            uniq.append(k)
    return uniq


# ---------- Record building ----------
def _fmt_dt_for_name(dt: datetime) -> str:
    # safe for Windows file names
    return dt.strftime("%Y-%m-%d_%H-%M-%S")


def _record_from_img(img: BaseImage, prefix: str, child_label: Optional[str]) -> Dict:
    file_ext = os.path.splitext(urlparse(img.url).path)[1].lower() or ".jpg"
    safe_prefix = _safe_name(prefix or "photo")
    filename = f"{safe_prefix}-{_fmt_dt_for_name(img.date)}-{img.img_id}{file_ext}"
    return {
        "id": str(img.img_id),
        "createdAt": img.date.astimezone(timezone.utc).isoformat(),
        "url": img.url,
        "filename": filename,
        "childLabel": child_label,
        "text": getattr(img, "text", None),
    }


# ---------- Source iterators ----------
def _iter_tagged_for_child(session: Session, child_id: str, child_label: str) -> Iterator[dict]:
    imgs = session.client.make_api_request(
        "GET", "/api/v2/images/tagged", params={"childId": child_id}
    ) or []
    for img_dict in imgs:
        img = PublicImage.from_dict(img_dict)
        # prefix just child name (matches CLI)
        yield _record_from_img(img, f"{child_label}", child_label)


def _iter_messages(session: Session) -> Iterator[dict]:
    conv_ids = session.client.make_api_request("GET", "/api/v2/conversations") or []
    for conv in reversed(conv_ids):
        conversation = session.client.make_api_request(
            "GET", f"/api/v2/conversations/{conv['conversationId']}"
        )
        for msg in reversed(conversation.get("messages", [])):
            text = f"{msg['body']} - {msg['author']['title']}"
            date = msg["createdAt"]
            for img_dict in msg.get("images", []):
                img = PublicImage.from_dict(img_dict, date_override=date, text_override=text)
                yield _record_from_img(img, "message", None)


def iter_photos_meta(session: Session, kind: str = "journey",
                     child_label: Optional[str] = None) -> Iterator[Dict]:
    """
    kind: "tagged" | "journey" | "notes" | "messages"
    Yields dicts with keys: id, createdAt, url, filename, childLabel, text (optional)
    """

    # messages doesn't depend on children
    if kind == "messages":
        yield from _iter_messages(session)
        return

    kids = list_children_best_effort(session)
    if not kids:
        raise RuntimeError("No children found via /api/me/me/me.")

    # If a specific child was selected in the UI, filter to that child only.
    # IMPORTANT: Do not override the label for all; filter the list instead.
    if child_label:
        kids = [k for k in kids if (k.get("label") == child_label)]
        if not kids:
            raise RuntimeError(f'No child named "{child_label}" found.')

    if kind == "tagged":
        # Iterate only the (possibly filtered) kids, and use each kid's own label
        for child in kids:
            cid = child["id"]
            clabel = child["label"] or "child"
            yield from _iter_tagged_for_child(session, cid, clabel)
        return

    if kind == "journey":
        for child in kids:
            cid = child["id"]
            clabel = child["label"] or "child"
            next_cursor = None
            while True:
                batch = session.client.learning_journey_query(
                    cid, cursor=next_cursor, first=100
                )
                for obs in batch["results"]:
                    text = f"{obs['remark']['body']} - {obs['createdBy']['name']['fullName']}"
                    date_str = obs["status"]["createdAt"]
                    for img_dict in obs["images"]:
                        img = SecretImage.from_dict(
                            img_dict, date_override=date_str, text_override=text
                        )
                        yield _record_from_img(img, f"{clabel}-journey", clabel)
                next_cursor = batch.get("next")
                if not next_cursor:
                    break
        return

    if kind == "notes":
        for child in kids:
            cid = child["id"]
            clabel = child["label"] or "child"
            next_ref = None
            while True:
                batch = session.client.get_child_notes(cid, cursor=next_ref, first=100)
                for note in batch["result"]:
                    text = f"{note['text']} - {note['createdBy']['name']['fullName']}"
                    date_str = note["createdAt"]
                    for img_dict in note["images"]:
                        img = SecretImage.from_dict(
                            img_dict, date_override=date_str, text_override=text
                        )
                        yield _record_from_img(img, f"{clabel}-note", clabel)
                next_ref = batch.get("next")
                if not next_ref:
                    break
        return

    # unknown kind
    return



# ---------- Download (matches downloader.fetch_image + EXIF) ----------
def download_photo(session: Session, photo: Dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / photo["filename"]

    req = urllib.request.Request(url=photo["url"])
    with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
        if r.status != 200:
            raise Exception(f"Broken! {r.read().decode('utf-8')}")
        shutil.copyfileobj(r, f)

    # Only try EXIF for JPEG/TIFF – keep file even if EXIF fails
    try:
        piexif.load(str(dest.resolve()))
    except piexif.InvalidImageDataError:
        return dest

    try:
        dt = datetime.fromisoformat(photo["createdAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
        exif_dict = {
            "Exif": {piexif.ExifIFD.DateTimeOriginal: dt.strftime("%Y:%m:%d %H:%M:%S").encode()}
        }
        if photo.get("text"):
            exif_dict["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(
                photo["text"], encoding="unicode"
            )
        exif_bytes = piexif.dump(exif_dict)
        try:
            piexif.insert(exif_bytes, str(dest.resolve()))
        except Exception as ex:
            print("EXIF insert failed:", ex)
    except Exception as ex:
        print("EXIF build failed:", ex)

    return dest
