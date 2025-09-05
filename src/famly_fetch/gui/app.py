# src/famly_fetch/gui/app.py
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone, timedelta
import os
import sys
import subprocess

import streamlit as st
import keyring

from famly_fetch.gui.adapter import (
    login_and_get_session,
    list_children_best_effort,
    iter_photos_meta,
    download_photo,
)
from famly_fetch.gui.state_store import StateStore

APP_NAME    = "Famly Photos (GUI)"
DEFAULT_DIR = Path.home() / "Pictures" / "FamlyPhotos"
STATE_DIR   = Path.home() / ".famly_fetcher"
STATE_FILE  = STATE_DIR / "state.json"

# ---------- Streamlit page ----------
st.set_page_config(page_title=APP_NAME, layout="centered")
st.title(APP_NAME)
st.caption("Downloads child-tagged photos from Famly. Password is never stored.")

# ---------- Session state defaults ----------
st.session_state.setdefault("connected", False)
st.session_state.setdefault("session", None)
st.session_state.setdefault("children", [])
st.session_state.setdefault("cancel", False)
st.session_state.setdefault("email", keyring.get_password("famly_fetcher", "email") or "")
st.session_state.setdefault("out_dir", str(DEFAULT_DIR))
st.session_state.setdefault("date_filter", "Last 30 days")
st.session_state.setdefault("only_new", True)
st.session_state.setdefault("login_msg", "")

# ---------- Login form (Tagged only; no Sources UI) ----------
with st.form("login", clear_on_submit=False):
    email = st.text_input("Email", st.session_state["email"], autocomplete="email")
    password = st.text_input("Password", type="password")
    out_dir = st.text_input("Save to folder", st.session_state["out_dir"])

    colA, colB = st.columns(2)
    with colA:
        date_filter = st.radio(
            "Date range", ["Last 30 days", "All time"],
            index=0 if st.session_state["date_filter"] == "Last 30 days" else 1,
            horizontal=True
        )
    with colB:
        only_new = st.checkbox("Only new since last run", value=st.session_state["only_new"])

    connect = st.form_submit_button("Connect")

if connect:
    # persist simple prefs
    st.session_state["email"] = email
    st.session_state["out_dir"] = out_dir
    st.session_state["date_filter"] = date_filter
    st.session_state["only_new"] = only_new

    # Remember email only (never password)
    try:
        keyring.set_password("famly_fetcher", "email", email)
    except Exception:
        pass

    # Attempt login once, store the live session for future reruns
    try:
        st.session_state["login_msg"] = "Logging in…"
        sess = login_and_get_session(email, password)
        kids = list_children_best_effort(sess)
        st.session_state["session"]  = sess
        st.session_state["children"] = kids
        st.session_state["connected"] = True
        st.session_state["login_msg"] = "Connected."
    except Exception as e:
        st.session_state["session"]  = None
        st.session_state["children"] = []
        st.session_state["connected"] = False
        st.session_state["login_msg"] = f"Login failed: {e}"

# ---------- Connection status ----------
if st.session_state["login_msg"]:
    if st.session_state["connected"]:
        st.success(st.session_state["login_msg"])
    else:
        st.error(st.session_state["login_msg"])
else:
    st.info("Not connected.")

# ---------- Early exit until connected ----------
if not st.session_state["connected"]:
    st.stop()

# ---------- Paths / state store ----------
out_path = Path(st.session_state["out_dir"]).expanduser()
out_path.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)
store = StateStore(STATE_FILE)
user_key = st.session_state["email"]

# ---------- Child selection (optional; default All) ----------
children = st.session_state["children"] or []
child_names = ["All children"] + [c["label"] for c in children]
child_choice = st.selectbox("Child", options=child_names)

# ---------- Date filters ----------
end = datetime.now(timezone.utc)
start = (end - timedelta(days=30)) if st.session_state["date_filter"] == "Last 30 days" else None

# ---------- Action buttons (outside any form) ----------
c1, c2, c3 = st.columns([1, 1, 2])
start_btn  = c1.button("Start download", use_container_width=True)
cancel_btn = c2.button("Cancel", use_container_width=True)
open_btn   = c3.button("Open folder", use_container_width=True)

# open folder cross-platform
if open_btn:
    try:
        out_path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(out_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(out_path)])
        else:
            subprocess.Popen(["xdg-open", str(out_path)])
    except Exception as e:
        st.error(f"Could not open folder: {e}")

# cancel flag
if cancel_btn:
    st.session_state["cancel"] = True
else:
    # pressing any other button clears stale cancel flag
    st.session_state["cancel"] = False

# if the user hasn't pressed Start yet, stop here (page stays interactive)
if not start_btn:
    st.stop()

# ---------- Helpers ----------
def parse_iso_utc(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)

def should_skip(photo: dict) -> bool:
    if start:
        try:
            ts = parse_iso_utc(photo["createdAt"])
            if ts < start:
                return True
        except Exception:
            pass
    if child_choice != "All children" and photo.get("childLabel") != child_choice:
        return True
    # de-dupe key includes source kind (tagged-only here)
    if st.session_state["only_new"] and store.seen(user_key, f"tagged:{photo['id']}"):
        return True
    return False

# ---------- Download loop (Tagged only) ----------
progress = st.progress(0, text="Preparing…")
log = st.empty()
downloaded = 0
skipped = 0
errors = 0
total_hint = 0
idx = 0

try:
    iterator = iter_photos_meta(
        st.session_state["session"],
        kind="tagged",
        child_label=(child_choice if child_choice != "All children" else None),
    )

    for photo in iterator:
        if st.session_state.get("cancel"):
            break
        idx += 1
        total_hint = max(total_hint, idx + 25)  # keep progress moving without a fixed total

        if should_skip(photo):
            skipped += 1
        else:
            try:
                saved_path = download_photo(st.session_state["session"], photo, out_path)
                # backward-compatible: if adapter returns None, fall back to expected path
                if not isinstance(saved_path, Path):
                    saved_path = out_path / photo["filename"]

                store.mark(user_key, f"tagged:{photo['id']}")
                downloaded += 1

                # verify on disk and show size
                try:
                    if saved_path.exists():
                        size = saved_path.stat().st_size
                        if size == 0:
                            log.write(f"⚠ Saved empty file: {saved_path.name}")
                        else:
                            log.write(f"✓ Saved {saved_path.name} ({size} bytes)")
                    else:
                        log.write(f"⚠ Expected file missing: {saved_path.name}")
                except Exception as fs_e:
                    log.write(f"⚠ Could not stat saved file: {fs_e}")

            except Exception as e:
                errors += 1
                log.write(f"Error on {photo.get('id')}: {e}")

        pct = min(100, int(100 * (idx / total_hint)))
        progress.progress(pct, text=f"{downloaded} downloaded • {skipped} skipped • {errors} errors")

finally:
    store.save()

if st.session_state.get("cancel"):
    progress.progress(100, text="Cancelled")
    st.warning(f"Cancelled. Downloaded {downloaded}, skipped {skipped}, errors {errors}.")
else:
    progress.progress(100, text="Done")
    st.success(f"Done! Downloaded {downloaded}, skipped {skipped}, errors {errors}. Saved to {out_path}")
