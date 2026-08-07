#!/usr/bin/env bash
# verify-restore-key-from-bao.sh — KEDGE-W-008, erweitert KEDGE-W-013 Folge
#
# Wrapper um verify.sh: zieht den Storage-Box-Subaccount-Private-Key UND das
# restic-Repo-Passwort fuer den quartalsweisen Test-Restore aus OpenBao statt
# aus einer lokalen Datei/manuellem SSH-Griff auf prod-cloud. Beide liegen
# nirgends dauerhaft auf der Maschine — nur fuer die Laufzeit dieses Scripts
# in 0600-Temp-Dateien, danach geshreddet (auch bei Fehlern, via trap).
#
# Voraussetzung: role_id/secret_id der AppRole `byrd-kedge-restore` liegen im
# macOS-Keychain (service "kedge-restore-approle-role-id"/"-secret-id", account
# "byrd") — AppRole ist per secret_id_bound_cidrs/token_bound_cidrs an die
# WooAir-WAN-IP gebunden, read-only auf genau EINEN KV-Pfad
# (secret/kedge/storage-box-restore-ewh-prod), der seit KEDGE-W-013 zwei
# Felder traegt: `private_key` (Storage-Box-SFTP) und `restic_password`
# (Repo-Entschluesselung — vorher musste das per Read-only-SSH live aus
# prod-cloud .kedge.env gezogen werden, KEDGE-W-013-Live-Validierung
# 2026-08-07, "Offen"-Punkt).
#
# Usage: ./verify-restore-key-from-bao.sh [weitere verify.sh-Argumente/Env]
#   Beispiel: RESTIC_REPOSITORY=sftp:u564740-sub1@u564740-sub1.your-storagebox.de:. \
#             RESTIC_NO_LOCK=1 ./verify-restore-key-from-bao.sh
#   Repo-Pfad MUSS die relative Form (":.") haben — der Subaccount ist auf
#   ewh-prod gechrootet und Hetzners sftp-Server loest ":/" nicht auf den
#   Chroot-Root auf (Lstat /config: file does not exist, KEDGE-W-013 Bug 2).
#   RESTIC_NO_LOCK=1, weil der Subaccount readonly ist — restic will sonst
#   ein Lock-File schreiben und scheitert schon beim Lesen.
#   RESTIC_PASSWORD/RESTIC_PASSWORD_FILE NICHT mehr selbst setzen — kommt seit
#   KEDGE-W-013 aus demselben KV-Feld wie der Key, wird hier ueberschrieben.

set -euo pipefail

BAO_ADDR="${BAO_ADDR:-https://bao.authbox.org}"
KV_PATH="${KV_PATH:-secret/kedge/storage-box-restore-ewh-prod}"
KEDGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TMPKEY="$(mktemp)"
TMPPW="$(mktemp)"
TMPROLE="$(mktemp)"
TMPSECRET="$(mktemp)"
cleanup() { rm -f "$TMPKEY" "$TMPPW" "$TMPROLE" "$TMPSECRET"; }
trap cleanup EXIT

# role_id/secret_id ueber @file statt Argv reinreichen — sonst stehen sie kurz
# im Klartext in der Prozessliste (ps aux), sichtbar fuer andere Prozesse auf
# derselben Maschine. Gleiches 0600+Shred-Muster wie beim Private Key selbst.
# printf statt Redirect: security -w haengt einen Newline an, der roh in der
# Datei den Wert verfaelschen wuerde (Login schlaegt fehl) — Command-Substitution
# strippt den trailing Newline, printf '%s' schreibt ihn nicht wieder rein.
role_id="$(security find-generic-password -s "kedge-restore-approle-role-id" -a "byrd" -w)"
secret_id="$(security find-generic-password -s "kedge-restore-approle-secret-id" -a "byrd" -w)"
chmod 600 "$TMPROLE" "$TMPSECRET"
printf '%s' "$role_id" > "$TMPROLE"
printf '%s' "$secret_id" > "$TMPSECRET"
unset role_id secret_id

# -format=json + .auth-Extraktion statt -field=client_token: bei Login-Endpoints
# liegt das Token unter .auth.*, nicht .data.* — `-field` sucht nur in .data und
# bricht mit 'Field "client_token" not present in secret' ab (KEDGE-W-013 Bug 1).
client_token="$(BAO_ADDR="$BAO_ADDR" bao write -format=json auth/approle/login \
  role_id="@$TMPROLE" secret_id="@$TMPSECRET" \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["auth"]["client_token"])')"

BAO_ADDR="$BAO_ADDR" BAO_TOKEN="$client_token" bao kv get -field=private_key "$KV_PATH" > "$TMPKEY"
chmod 600 "$TMPKEY"

BAO_ADDR="$BAO_ADDR" BAO_TOKEN="$client_token" bao kv get -field=restic_password "$KV_PATH" > "$TMPPW"
chmod 600 "$TMPPW"
unset client_token

export RESTIC_SFTP_KEY="$TMPKEY"
export RESTIC_PASSWORD_FILE="$TMPPW"
"$KEDGE_DIR/verify.sh" "$@"
# kein exec: der EXIT-trap (Key-Shred) muss in DIESEM Prozess feuern, nicht im
# ersetzten verify.sh-Image.
