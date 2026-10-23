#!/usr/bin/env python3
"""
Alfred Cloud Bot H24 (Eseguito su GitHub Actions quando il PC è spento)
-----------------------------------------------------------------------
- Risponde su @Alfred_agy_bot usando Gemini 3.8 Flash (multimodale: testo + immagini).
- Crea e modifica progetti direttamente nel repository privato 'progetti-ai-vault',
  così appena accendi il PC vengono scaricati in automatico in Desktop\\Progetti AI!
- Chiede SEMPRE il permesso con pulsanti interattivi prima di inviare qualsiasi file su Telegram.
- Si ferma automaticamente appena rileva che il PC è acceso (pc_online = True).
"""

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
ALLOWED_CHAT_ID = int(os.environ["ALLOWED_CHAT_ID"].strip().strip("'").strip('"'))
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
GH_PAT = os.environ["GH_PAT"].strip()
GH_USER = os.environ.get("ALFRED_GH_USER", "ProgettoAlfred").strip()
VAULT_REPO = os.environ.get("ALFRED_VAULT_REPO", "progetti-ai-vault").strip()
ENGINE_REPO = os.environ.get("ALFRED_ENGINE_REPO", "alfred-cloud-engine").strip()
GOOGLE_HUB_URL = os.environ.get("GOOGLE_HUB_URL", "").strip()
ALFRED_SECRET = "Alfred_Pasquale_2026_Secret"

WORK_DIR = Path("workspace")
WORK_DIR.mkdir(exist_ok=True)


def api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def gh_headers() -> dict:
    return {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github+json",
    }


def send_text(text: str, reply_markup: dict | None = None):
    payload = {"chat_id": ALLOWED_CHAT_ID, "text": text[:4000]}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(api_url("sendMessage"), json=payload, timeout=20)


def send_file_telegram(file_path: Path, caption: str = ""):
    with open(file_path, "rb") as f:
        requests.post(
            api_url("sendDocument"),
            data={"chat_id": ALLOWED_CHAT_ID, "caption": caption[:1000]},
            files={"document": (file_path.name, f)},
            timeout=180,
        )


def get_vault_json(path: str, default: dict) -> dict:
    url = f"https://api.github.com/repos/{GH_USER}/{VAULT_REPO}/contents/{path.lstrip('/')}"
    r = requests.get(url, headers=gh_headers(), timeout=15)
    if r.status_code == 200:
        try:
            content = base64.b64decode(r.json()["content"]).decode("utf-8")
            return json.loads(content)
        except Exception:
            pass
    return default


def put_vault_file(path: str, data: bytes, msg: str):
    url = f"https://api.github.com/repos/{GH_USER}/{VAULT_REPO}/contents/{path.lstrip('/')}"
    r = requests.get(url, headers=gh_headers(), timeout=15)
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": msg, "content": base64.b64encode(data).decode("ascii")}
    if sha:
        payload["sha"] = sha
    requests.put(url, headers=gh_headers(), json=payload, timeout=30)


def list_vault_projects() -> list[str]:
    url = f"https://api.github.com/repos/{GH_USER}/{VAULT_REPO}/contents/projects"
    r = requests.get(url, headers=gh_headers(), timeout=15)
    if r.status_code != 200 or not isinstance(r.json(), list):
        return []
    return sorted([item["name"] for item in r.json() if item.get("type") == "dir"])


def call_gemini(
    prompt_text: str,
    active_project: str,
    image_bytes: bytes | None = None,
    audio_bytes: bytes | None = None,
    audio_mime: str = "audio/ogg",
) -> dict:
    """
    Chiama Gemini Flash chiedendo una risposta JSON strutturata che può includere:
    - reply: risposta testuale per l'utente su Telegram
    - files_to_save: lista di {"path": "percorso/relativo", "content": "..."} da salvare nel progetto attivo
    - shell_cmd: eventuale comando per compilare/creare un file (es. zip/apk/exe)
    - generated_file: percorso di un file generato (per cui chiedere SEMPRE conferma prima dell'invio!)
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    system_prompt = (
        f"Sei Alfred (versione Cloud H24 di Antigravity) per Pasquale (data/ora attuale: {now_str}). "
        f"Il suo PC di casa al momento è spento, quindi stai lavorando nel Cloud sul progetto attivo '{active_project}'. "
        f"Tutti i file che crei in 'files_to_save' verranno salvati nel Cloud e sincronizzati automaticamente "
        f"sul suo PC in 'C:\\Users\\pasqu\\Desktop\\Progetti AI\\{active_project}' appena accenderà il PC.\n"
        f"Hai anche accesso diretto a tutti i servizi Google di Pasquale (Google Calendar, Google Tasks/Keep, Google Drive, Google Docs, Gmail) "
        f"tramite il campo opzionale 'google_actions' (lista di azioni da eseguire sul suo account Google).\n"
        f"Azioni supportate in 'google_actions':\n"
        f"- {{\"action\": \"calendar_add\", \"title\": \"...\", \"start_iso\": \"2026-11-30T09:00:00+01:00\", \"end_iso\": \"2026-11-30T10:00:00+01:00\", \"description\": \"...\", \"location\": \"...\"}}\n"
        f"- {{\"action\": \"note_add\", \"title\": \"...\", \"content\": \"...\", \"due_iso\": \"2026-11-30T09:00:00Z\"}}\n"
        f"- {{\"action\": \"doc_create\", \"title\": \"...\", \"content\": \"...\"}}\n"
        f"- {{\"action\": \"gmail_send\", \"to\": \"...\", \"subject\": \"...\", \"body\": \"...\"}}\n"
        f"REGOLA FONDAMENTALE SUI FILE: Non inviare mai file in automatico. Se generi un file compilato o un pacchetto, "
        f"indicalo in 'generated_file' e nel testo 'reply' chiedi a Pasquale se vuole riceverlo su Telegram.\n"
        f"Rispondi ESCLUSIVAMENTE con un oggetto JSON valido con questa struttura:\n"
        f'{{"reply": "tua risposta in italiano...", "files_to_save": [{{"path": "nomefile.ext", "content": "codice..."}}], '
        f'"google_actions": [], "shell_cmd": "", "generated_file": ""}}'
    )

    parts = [{"text": f"{system_prompt}\n\nRichiesta di Pasquale:\n{prompt_text}"}]
    if image_bytes:
        parts.append(
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            }
        )
    if audio_bytes:
        parts.append(
            {
                "inline_data": {
                    "mime_type": audio_mime,
                    "data": base64.b64encode(audio_bytes).decode("ascii"),
                }
            }
        )

    models_to_try = ["gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest", "gemini-3-flash-preview"]

    last_err = ""
    for m_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={GEMINI_API_KEY}"
        resp = requests.post(
            url,
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
            timeout=90,
        )
        if resp.status_code == 200:
            cand = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            try:
                return json.loads(cand)
            except Exception:
                return {"reply": cand, "files_to_save": [], "shell_cmd": "", "generated_file": ""}
        last_err = f"{resp.status_code}: {resp.text[:300]}"

    return {
        "reply": f"⚠️ Errore chiamata Gemini API: {last_err}",
        "files_to_save": [],
        "shell_cmd": "",
        "generated_file": "",
    }


FORCE_TAKEOVER = os.environ.get("FORCE_TAKEOVER", "").strip()  # "shutdown" / "sleep" se avviato dallo spegnimento del PC
try:
    FORCE_TS = float(os.environ.get("FORCE_TS", "0").strip() or 0)
except Exception:
    FORCE_TS = 0.0
_stale_takeover = False


def is_pc_truly_online(pc_status: dict) -> bool:
    """
    Il PC è considerato acceso SOLO se ha inviato un battito cardiaco recente (< 75s).
    Se il Cloud è stato avviato dallo spegnimento del PC (FORCE_TAKEOVER), il PC è considerato
    acceso solo se ha inviato un battito DOPO l'istante dello spegnimento (es. riacceso).
    """
    global _stale_takeover
    if not pc_status.get("pc_online"):
        return False
    hb_raw = pc_status.get("last_heartbeat_utc") or pc_status.get("updated_at") or ""
    if not hb_raw:
        return False
    try:
        hb_dt = datetime.fromisoformat(hb_raw.replace("Z", "+00:00"))
        if FORCE_TAKEOVER and FORCE_TS and hb_dt.timestamp() <= FORCE_TS + 5:
            return False
        age_sec = (datetime.now(timezone.utc) - hb_dt).total_seconds()
        # Se il PC non invia un battito da più di 75 secondi, è spento/offline/in sospensione!
        if age_sec > 75:
            print(f"Heartbeat PC scaduto ({age_sec:.0f}s fa): subentro automatico del Cloud!")
            pc_status["pc_online"] = False
            put_vault_file("status/pc_status.json", json.dumps(pc_status).encode(), "Auto-failover to Cloud (stale heartbeat)")
            _stale_takeover = True
            return False
    except Exception:
        pass
    return True


def main():
    start_time = time.time()
    max_run_seconds = 45 * 60  # Gira per 45 minuti e poi si riavvia da solo se il PC è ancora spento

    pc_status = get_vault_json("status/pc_status.json", {"pc_online": False})
    if is_pc_truly_online(pc_status):
        print("PC rilevato ONLINE (battito attivo)! Il bot Cloud non parte per lasciare il posto ad Antigravity Pro.")
        return

    cloud_state = get_vault_json(
        "status/cloud_state.json",
        {"last_update_id": 0, "active_project": "telegram-bridge", "pending_file": ""},
    )
    if pc_status.get("active_project_name"):
        cloud_state["active_project"] = pc_status["active_project_name"]

    last_known_id = max(
        int(cloud_state.get("last_update_id", 0)),
        int(pc_status.get("last_update_id", 0)),
    )
    offset = last_known_id + 1 if last_known_id > 0 else 0

    should_notify = bool(pc_status.get("notify_start") or _stale_takeover or pc_status.get("pc_online"))
    if FORCE_TAKEOVER:
        last_force = float(cloud_state.get("last_force_ts", 0) or 0)
        # Più avvii ravvicinati per lo stesso spegnimento (guardiano + operazione pianificata) = 1 solo messaggio
        if FORCE_TS > last_force + 120:
            should_notify = True
        cloud_state["last_force_ts"] = max(FORCE_TS, last_force)
        put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update cloud state")

    if should_notify:
        motivo = {
            "shutdown": "Il PC si è appena spento",
            "sleep": "Il PC è andato in sospensione",
        }.get(FORCE_TAKEOVER, "Il PC risulta offline o spento")
        pc_status["notify_start"] = False
        pc_status["pc_online"] = False
        put_vault_file("status/pc_status.json", json.dumps(pc_status).encode(), "Ack cloud start")
        send_text(
            f"☁️ [Alfred Cloud H24 — Switch da PC a Cloud completato]\n\n"
            f"{motivo}: ho effettuato lo switch da PC a Cloud H24.\n"
            f"Sono in ascolto dai server GitHub (progetto attivo: '{cloud_state.get('active_project', 'telegram-bridge')}').\n"
            f"• Scrivimi pure una domanda, un codice, un vocale o mandami un'immagine!\n"
            f"• Usa /nuovo <nome> per creare un progetto nel Cloud.\n"
            f"• Non appena accendi il PC, il controllo tornerà automaticamente ad Antigravity Pro con relativo messaggio di switch."
        )


    while time.time() - start_time < max_run_seconds:
        # 1. Controlla se il PC è acceso: se sì, cede subito il controllo ad Antigravity Pro sul PC!
        pc_status = get_vault_json("status/pc_status.json", {"pc_online": False})
        if is_pc_truly_online(pc_status):
            print("PC rilevato ONLINE! Il bot Cloud si mette in pausa per lasciare il posto ad Antigravity Pro.")
            return

        try:
            r = requests.get(
                api_url("getUpdates"),
                params={"offset": offset, "timeout": 20},
                timeout=30,
            )
            data = r.json()
            if not data.get("ok"):
                time.sleep(3)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                cloud_state["last_update_id"] = upd["update_id"]

                # Gestione conferma invio file su Telegram
                cb = upd.get("callback_query")
                if cb:
                    cb_data = cb.get("data", "")
                    requests.post(api_url("answerCallbackQuery"), json={"callback_query_id": cb["id"]}, timeout=10)
                    if cb_data == "cloud_send_file:yes":
                        pfile = cloud_state.get("pending_file", "")
                        if pfile and Path(pfile).exists():
                            send_text(f"📤 Invio di {Path(pfile).name} in corso...")
                            send_file_telegram(Path(pfile), f"📦 {Path(pfile).name} (dal Cloud)")
                        else:
                            send_text("⚠️ Nessun file in attesa di invio.")
                    elif cb_data == "cloud_send_file:no":
                        send_text("👍 Perfetto, non lo invio su Telegram. Resterà sincronizzato per il tuo PC!")
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update cloud state")
                    continue

                msg = upd.get("message")
                if not msg or msg.get("chat", {}).get("id") != ALLOWED_CHAT_ID:
                    continue

                text = (msg.get("text") or msg.get("caption") or "").strip()
                active_proj = cloud_state.get("active_project", "telegram-bridge")

                # Se il messaggio è un comando vecchio inviato prima dell'avvio di questo Cloud bot, ignoralo
                msg_date = msg.get("date", 0)
                if msg_date and (msg_date < start_time - 120) and text.startswith("/"):
                    print(f"Ignorato vecchio comando Telegram obsoleto ({text}) inviato alle {msg_date}")
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update cloud state")
                    continue

                # Ritorno in modalità PC
                if text.lower().startswith("/pc"):
                    fresh_pc = get_vault_json("status/pc_status.json", {"pc_online": False})
                    if is_pc_truly_online(fresh_pc):
                        put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update cloud state")
                        send_text(
                            "💻 Antigravity Pro sul tuo PC è attivo e connesso: passo subito il controllo al PC!"
                        )
                        return
                    else:
                        send_text(
                            "⚠️ [Cloud H24] Il tuo PC risulta attualmente SPENTO o OFFLINE.\n\n"
                            "Resto attivo io nel Cloud per eseguire le tue richieste!\n"
                            "Non appena accenderai il PC (anche da remoto), Antigravity Pro subentrerà automaticamente."
                        )
                        put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update cloud state")
                        continue

                # Comandi rapidi Cloud
                if text.lower().startswith("/progetto"):
                    send_text(
                        f"☁️ [Modalità Cloud H24]\n"
                        f"📂 Progetto attivo: {active_proj}\n"
                        f"📍 Verrà sincronizzato sul PC in: C:\\Users\\pasqu\\Desktop\\Progetti AI\\{active_proj}"
                    )
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update state")
                    continue

                if text.lower().startswith("/progetti"):
                    projs = list_vault_projects()
                    lines = ["☁️ [Modalità Cloud] Progetti sincronizzati nel Vault:"]
                    for p in projs:
                        m = "✅" if p == active_proj else "▫️"
                        lines.append(f"{m} {p}")
                    send_text("\n".join(lines))
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update state")
                    continue

                if text.lower().startswith("/sync"):
                    send_text(
                        f"☁️ [Modalità Cloud] Tutti i file del progetto '{active_proj}' sono già salvati nel tuo Vault su GitHub e verranno scaricati in Desktop\\Progetti AI appena torni al PC (o tocchi /pc)!"
                    )
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update state")
                    continue

                if text.lower().startswith(("/limiti", "/modello")):
                    send_text(
                        "☁️ [Modalità Cloud H24]\n"
                        "In questo momento ti sto rispondendo dal Cloud tramite Gemini 2.5 Flash API (quindi NON stai consumando i limiti di Antigravity Pro!).\n"
                        "Per interrogare i limiti live o cambiare il modello di Antigravity sul PC, tocca prima /pc."
                    )
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update state")
                    continue

                if text.lower().startswith("/help"):
                    send_text(
                        "☁️ [Guida Modalità Cloud H24]:\n"
                        "• Scrivimi qualsiasi domanda o codice da creare\n"
                        "• /nuovo <nome> — Crea un nuovo progetto nel Cloud\n"
                        "• /progetti — Vedi i progetti nel Vault\n"
                        "• /usa <nome> — Cambia progetto attivo\n"
                        "• /pc — Restituisci subito il controllo ad Antigravity Pro sul PC"
                    )
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update state")
                    continue

                if text.lower().startswith("/nuovo"):
                    arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
                    if not arg:
                        send_text("⚠️ Specifica il nome del progetto: es. /nuovo app-spese")
                    else:
                        proj_name = Path(arg.replace("\\", "/")).name
                        cloud_state["active_project"] = proj_name
                        put_vault_file(
                            f"projects/{proj_name}/README.md",
                            f"# {proj_name}\nCreato da Alfred Cloud H24.\n".encode("utf-8"),
                            f"Create project {proj_name}",
                        )
                        send_text(
                            f"☁️ ✨ Nuovo progetto '{proj_name}' creato nel Cloud!\n"
                            f"Appena accenderai il PC verrà scaricato automaticamente in:\n"
                            f"📍 C:\\Users\\pasqu\\Desktop\\Progetti AI\\{proj_name}"
                        )
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update state")
                    continue

                if text.lower().startswith("/usa"):
                    arg = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
                    if arg:
                        cloud_state["active_project"] = arg
                        send_text(f"☁️ 🔄 Progetto attivo nel Cloud impostato su: {arg}")
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update state")
                    continue

                # Scarica eventuale foto inviata
                img_bytes = None
                if "photo" in msg and msg["photo"]:
                    fid = msg["photo"][-1]["file_id"]
                    finfo = requests.get(api_url("getFile"), params={"file_id": fid}, timeout=15).json()
                    if finfo.get("ok"):
                        rpath = finfo["result"]["file_path"]
                        img_bytes = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{rpath}", timeout=30).content
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        put_vault_file(
                            f"projects/{active_proj}/inbox_cloud/{ts}_photo.jpg",
                            img_bytes,
                            f"Save photo from Telegram to {active_proj}",
                        )

                # Scarica eventuale messaggio vocale o file audio inviato
                audio_bytes = None
                if "voice" in msg:
                    fid = msg["voice"]["file_id"]
                    finfo = requests.get(api_url("getFile"), params={"file_id": fid}, timeout=15).json()
                    if finfo.get("ok"):
                        rpath = finfo["result"]["file_path"]
                        audio_bytes = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{rpath}", timeout=30).content
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        put_vault_file(
                            f"projects/{active_proj}/inbox_cloud/{ts}_voice.ogg",
                            audio_bytes,
                            f"Save voice note from Telegram to {active_proj}",
                        )

                if "audio" in msg:
                    fid = msg["audio"]["file_id"]
                    finfo = requests.get(api_url("getFile"), params={"file_id": fid}, timeout=15).json()
                    if finfo.get("ok"):
                        rpath = finfo["result"]["file_path"]
                        audio_bytes = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{rpath}", timeout=30).content

                if audio_bytes:
                    send_text(f"🎙️ [{active_proj} - Cloud H24] Ho ricevuto il tuo messaggio vocale! Lo ascolto ed elaboro con Gemini...")
                    prompt_to_use = text or "Ascolta con la massima attenzione la richiesta vocale contenuta nell'audio allegato ed eseguila integralmente."
                    result = call_gemini(prompt_to_use, active_proj, image_bytes=img_bytes, audio_bytes=audio_bytes, audio_mime="audio/ogg")
                else:
                    send_text(f"☁️ [{active_proj} - Cloud H24] Elaboro la tua richiesta con Gemini...")
                    result = call_gemini(text or "Analizza l'immagine allegata", active_proj, image_bytes=img_bytes)


                proj_local = WORK_DIR / active_proj
                proj_local.mkdir(parents=True, exist_ok=True)
                saved_files = []
                for fitem in result.get("files_to_save") or []:
                    rel_p = fitem.get("path", "").lstrip("/")
                    fcontent = fitem.get("content", "")
                    if rel_p:
                        local_f = proj_local / rel_p
                        local_f.parent.mkdir(parents=True, exist_ok=True)
                        local_f.write_text(fcontent, encoding="utf-8")
                        put_vault_file(
                            f"projects/{active_proj}/{rel_p}",
                            fcontent.encode("utf-8"),
                            f"Cloud update {active_proj}/{rel_p}",
                        )
                        saved_files.append(rel_p)

                if result.get("shell_cmd"):
                    subprocess.run(result["shell_cmd"], shell=True, cwd=str(proj_local), timeout=180)

                google_results = []
                if GOOGLE_HUB_URL and result.get("google_actions"):
                    for gact in result.get("google_actions") or []:
                        if isinstance(gact, dict) and gact.get("action"):
                            gpayload = dict(gact)
                            gpayload["secret"] = ALFRED_SECRET
                            try:
                                gr = requests.post(GOOGLE_HUB_URL, json=gpayload, timeout=40, allow_redirects=True).json()
                                if gr.get("ok"):
                                    google_results.append(f"✅ Google ({gact['action']}): completato!")
                                else:
                                    google_results.append(f"⚠️ Google ({gact['action']}): {gr.get('error')}")
                            except Exception as ge:
                                google_results.append(f"⚠️ Errore Google ({gact['action']}): {ge}")

                reply_msg = f"☁️ [Alfred Cloud - {active_proj}]:\n{result.get('reply', 'Fatto!')}"
                if google_results:
                    reply_msg += "\n\n🔗 Azioni Google eseguite:\n• " + "\n• ".join(google_results)
                if saved_files:
                    reply_msg += (
                        f"\n\n💾 File salvati e pronti per la sincronizzazione sul PC "
                        f"(Desktop\\Progetti AI\\{active_proj}):\n• " + "\n• ".join(saved_files)
                    )

                gen_file = result.get("generated_file", "")
                if gen_file and (proj_local / gen_file).exists():
                    full_gen = str((proj_local / gen_file).resolve())
                    cloud_state["pending_file"] = full_gen
                    kb = {
                        "inline_keyboard": [
                            [
                                {"text": "✅ Sì, inviamelo su Telegram", "callback_data": "cloud_send_file:yes"},
                                {"text": "❌ No, solo su PC", "callback_data": "cloud_send_file:no"},
                            ]
                        ]
                    }
                    send_text(
                        reply_msg + f"\n\n📦 Il file '{gen_file}' è pronto: vuoi che te lo mandi qui su Telegram?",
                        reply_markup=kb,
                    )
                else:
                    send_text(reply_msg)

                put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update cloud state")

        except Exception as e:
            print(f"Errore ciclo Cloud: {e}")
            time.sleep(5)

    # Se il ciclo di 45 minuti termina e il PC è ancora spento, riavvia automaticamente il workflow!
    pc_status = get_vault_json("status/pc_status.json", {"pc_online": False})
    if not pc_status.get("pc_online"):
        requests.post(
            f"https://api.github.com/repos/{GH_USER}/{ENGINE_REPO}/actions/workflows/alfred_cloud.yml/dispatches",
            headers=gh_headers(),
            json={"ref": "main"},
            timeout=15,
        )


if __name__ == "__main__":
    main()
