import os
import json
import base64
import logging
from typing import Optional

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

import google.auth.transport.requests as tr_requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import Flow
from email.mime.text import MIMEText


SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send"
]


KV_SECRET_NAME = os.environ.get("KV_SECRET_NAME", "GOOGLE_OAUTH_TOKEN")
KV_CREDENTIALS_NAME = os.environ.get("KV_CREDENTIALS_NAME", "GOOGLE_CREDENTIALS_JSON")
KV_URI = os.environ.get("KEYVAULT_URI")


def get_secret_client() -> SecretClient:
    if not KV_URI:
        raise Exception("KEYVAULT_URI environment variable is not set.")
    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=KV_URI, credential=credential)

    return client


def store_token_in_keyvault(token_json: str) -> None:
    client = get_secret_client()
    client.set_secret(KV_SECRET_NAME, token_json)


def read_token_from_keyvault() -> Optional[str]:
    client = get_secret_client()

    try:
        secret = client.get_secret(KV_SECRET_NAME)
        return secret.value
    except Exception as e:
        logging.info(f"No token in Key Vault: {e}")
        return None


def read_secret_from_keyvault(secret_name: str) -> Optional[str]:
    client = get_secret_client()

    try:
        secret = client.get_secret(secret_name)
        return secret.value
    except Exception as e:
        logging.info(f"No secret '{secret_name} found in Key Vault: {e}")
        return None


def build_credentials_from_token(token_json: str) -> Credentials:
    info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(info, scopes=SCOPES)
    request = tr_requests.Request()

    if not creds.valid() and creds.refresh_token:
        creds.refresh(request)
    return creds


def create_calendar_service(creds: Credentials):
    return build("calendar", "v3", credentials=creds)


def create_gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)


def check_availability(calendar_service, start_time_iso: str, end_time_iso: str) -> bool:
    body = {
        "timeMin": start_time_iso,
        "timeMax": end_time_iso,
        "items": [{"id": "primary"}]
    }

    response = calendar_service.freebusy().query(body=body).execute()
    busy = response.get("calendars", {}).get("primary", {}).get("busy", [])

    return len(busy) == 0


def create_event(calendar_service, summary: str, start_time_iso: str, end_time_iso: str, attendees):
    event = {
        "summary": summary,
        "start": {
            "dateTime": start_time_iso,
            "timeZone": "America/Sao Paulo"
        },
        "end": {
            "dateTime": end_time_iso,
            "timeZone": "America/Sao Paulo"
        },
        "attendees": [{"email": e} for e in attendees],
        "reminders": {"useDefault": True}
    }

    created = calendar_service.events().insert(calendarId="primary", body=event, sendUpdates="all").execute()

    return created


def send_email(gmail_service, to: str, subject: str, body_text: str):
    message = MIMEText(body_text)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()


def handle_oauth2init(req: func.HttpRequest) -> func.HttpResponse:
    host = req.headers.get("x-forwarded-host") or req.headers.get("Host")

    if not host:
        return func.HttpResponse("Cannot determine host for callback URL.", status_code=500)
    
    credentials_json_str = read_secret_from_keyvault(KV_CREDENTIALS_NAME)

    if not credentials_json_str:
        return func.HttpResponse("FATAL: Google credentials JSON not found in Key Vault.", status_code=500)

    try:
        with open("credentials.json", "w") as f:
            f.write(credentials_json_str)
    except Exception as e:
        logging.error(f"Failes to write credentials.json file: {e}")
        return func.HttpResponse("FATAL: Failed to write credentials.json file.", status_code=500)

    callback = f"https://{host}/api/oauth2callback"

    flow = Flow.from_client_secrets_file("credentials.json", scopes=SCOPES, redirect_uri=callback)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )

    return func.HttpResponse(status_code=302, headers={"Location": auth_url})


def handle_oauth2callback(req: func.HttpRequest) -> func.HttpResponse:
    host = req.headers.get("x-forwarded-host") or req.headers.get("Host")

    if not host:
        return func.HttpResponse("Cannot determine host for callback URL.", status_code=500)

    credentials_json_str = read_secret_from_keyvault(KV_CREDENTIALS_NAME)

    if not credentials_json_str:
        return func.HttpResponse("FATAL: Failed to write credentials.json file.", status_code=500)

    callback = f"https://{host}/api/oauth2callback"

    flow = Flow.from_client_secrets_file("credentials.json", scopes=SCOPES, redirect_uri=callback)
    flow.fetch_token(authorization_response=req.url)
    creds = flow.credentials
    token_json = creds.to_json()
    store_token_in_keyvault(token_json)

    return func.HttpResponse("OAuth complete - token saved in Key Vault. You can close this page.", status_code=200)


def handle_calendar_agent(req: func.HttpRequest) -> func.HttpResponse:
    try:
        data = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON body.", status_code=400)

    summary = data.get("summary", "Reunião")
    start = data.get("start")
    end = data.get("end")
    attendees = data.get("attendees", [])

    if not start or not end:
        return func.HttpResponse("Missing 'start' or 'end'.", status_code=400)

    token_json = read_token_from_keyvault()

    if not token_json:
        return func.HttpResponse("No OAuth token in Key Vault. Start OAuth at /api/oauth2init", status_code=400)

    creds = build_credentials_from_token(token_json)
    cal = create_calendar_service(creds)
    gmail = create_gmail_service(creds)
    available = check_availability(cal, start, end)

    if not available:
        return func.HttpResponse(json.dumps(
            {"status": "no_slots", "message": "Horário indisponível"}),
            status_code=200,
            mimetype="application/json"
        )

    event = create_event(cal, summary, start, end, attendees)

    for p in attendees:
        try:
            send_email(gmail, p, f"Confirmação: {summary}", f"O evento '{summary}' foi agendado para {start}.")
        except Exception as e:
            logging.warning(f"Failed sending email to {p}: {e}")

    result = {"status": "success", "event_link": event.get("htmlLink"), "id": event.get("id")}

    return func.HttpResponse(json.dumps(result), mimetype="application/json", status_code=200)


def main(req: func.HttpRequest) -> func.HttpResponse:
    action = req.route_params.get("action")

    if not action:
        return func.HttpResponse("Use /api/oauth2init, /api/oauth2callback or /api/calendar_agent", status_code=400)

    action = action.lower()

    if action == "oauth2init":
        return handle_oauth2init(req)

    if action == "oauth2callback":
        return handle_oauth2callback(req)

    if action in ("calendar_agent", "agenda", "schedule"):
        return handle_calendar_agent(req)

    return func.HttpResponse("Successfully executed.", status_code=200)
