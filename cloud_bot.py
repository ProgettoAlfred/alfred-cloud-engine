#!/usr/bin/env python3
"""
Alfred Cloud Bot H24 (Eseguito su GitHub Actions quando il PC è spento)
-----------------------------------------------------------------------
- Risponde su @Alfred_agy_bot usando Gemini 2.5 Flash (multimodale: testo + immagini).
- Crea e modifica progetti direttamente nel repository privato 'progetti-ai-vault',
  così appena accendi il PC vengono scaricati in automatico in Desktop\Progetti AI!
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


def call_gemini(prompt_text: str, active_project: str, image_bytes: bytes | None = None) -> dict:
    """
    Chiama Gemini 2.5 Flash chiedendo una risposta JSON strutturata che può includere:
    - reply: risposta testuale per l'utente su Telegram
    - files_to_save: lista di {"path": "percorso/relativo", "content": "..."} da salvare nel progetto attivo
    - shell_cmd: eventuale comando per compilare/creare un file (es. zip/apk/exe)
    - generated_file: percorso di un file generato (per cui chiedere SEMPRE conferma prima dell'invio!)
    """
    system_prompt = (
        f"Sei Alfred (versione Cloud H24 di Antigravity) per Pasquale. "
        f"Il suo PC di casa al momento è spento, quindi stai lavorando nel Cloud sul progetto attivo '{active_project}'. "
        f"Tutti i file che crei in 'files_to_save' verranno salvati nel Cloud e sincronizzati automaticamente "
        f"sul suo PC in 'C:\\Users\\pasqu\\Desktop\\Progetti AI\\{active_project}' appena accenderà il PC.\n"
        f"REGOLA FONDAMENTALE SUI FILE: Non inviare mai file in automatico. Se generi un file compilato o un pacchetto, "
        f"indicalo in 'generated_file' e nel testo 'reply' chiedi a Pasquale se vuole riceverlo su Telegram.\n"
        f"Rispondi ESCLUSIVAMENTE con un oggetto JSON valido con questa struttura:\n"
        f'{{"reply": "tua risposta in italiano...", "files_to_save": [{{"path": "nomefile.ext", "content": "codice..."}}], '
        f'"shell_cmd": "", "generated_file": ""}}'
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    resp = requests.post(
        url,
        json={
            "contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
        timeout=90,
    )
    resp.raise_for_status()
    cand = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    try:
        return json.loads(cand)
    except Exception:
        return {"reply": cand, "files_to_save": [], "shell_cmd": "", "generated_file": ""}


def main():
    start_time = time.time()
    max_run_seconds = 45 * 60  # Gira per 45 minuti e poi si riavvia da solo se il PC è ancora spento

    pc_status = get_vault_json("status/pc_status.json", {"pc_online": False})
    if pc_status.get("pc_online"):
        print("PC rilevato ONLINE! Il bot Cloud non parte per lasciare il posto ad Antigravity Pro.")
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

    if pc_status.get("notify_start"):
        pc_status["notify_start"] = False
        put_vault_file("status/pc_status.json", json.dumps(pc_status).encode(), "Ack cloud start")
        send_text(
            f"☁️ [Alfred Cloud H24 — Online!]\n"
            f"Sono in ascolto dai server GitHub (progetto attivo: '{cloud_state.get('active_project', 'telegram-bridge')}').\n"
            f"• Scrivimi pure una domanda o mandami un'immagine!\n"
            f"• Usa /nuovo <nome> per creare un progetto nel Cloud.\n"
            f"• Tocca /pc quando vuoi restituire il controllo ad Antigravity Pro sul tuo PC."
        )

    while time.time() - start_time < max_run_seconds:
        # 1. Controlla se il PC è acceso: se sì, cede subito il controllo ad Antigravity Pro sul PC!
        pc_status = get_vault_json("status/pc_status.json", {"pc_online": False})
        if pc_status.get("pc_online"):
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

                # Ritorno immediato in modalità PC
                if text.lower().startswith("/pc"):
                    put_vault_file("status/cloud_state.json", json.dumps(cloud_state).encode(), "Update cloud state")
                    put_vault_file(
                        "status/pc_status.json",
                        json.dumps(
                            {
                                "pc_online": True,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                                "active_project_name": active_proj,
                                "last_update_id": upd["update_id"],
                            }
                        ).encode(),
                        "Handover back to PC",
                    )
                    send_text(
                        "💻 Passo subito il controllo ad Antigravity Pro sul tuo PC e sincronizzo eventuali file creati nel Cloud..."
                    )
                    return

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

                send_text(f"☁️ [{active_proj} - Cloud H24] Elaboro la tua richiesta con Gemini...")
                result = call_gemini(text or "Analizza l'immagine allegata", active_proj, img_bytes)

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

                reply_msg = f"☁️ [Alfred Cloud - {active_proj}]:\n{result.get('reply', 'Fatto!')}"
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
