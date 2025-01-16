import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .utils import decode_base64, save_file

# 이 스코프를 수정하면 token.json 파일을 삭제해야 합니다.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailService:
    def __init__(self):
        self.service = self._authenticate_with_token()

    @staticmethod
    def _authenticate_with_token():
        """
        token.json을 사용하여 사용자 인증을 진행하거나 필요한 경우 OAuth2 흐름을 수행합니다.
        반환값:
            service (Resource): Gmail API 서비스 객체.
        """
        creds = None
        if os.path.exists("token.json"):
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
                creds = flow.run_local_server(port=8080, prompt="consent", access_type="offline")

            with open("token.json", "w") as token:
                token.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_today_n_messages(self, date: str, n: int = 100):
        """
        최근 N개의 메시지를 가져옵니다.
        인자:
            date (str): 년/월/일 형식의 가져오기 시작할 날짜 문자열.
            n (int): 가져올 메시지의 개수.
        반환값:
            messages (list): 메시지 메타데이터 목록.
        """
        message_list = self.service.users().messages().list(userId="me", maxResults=n, q=f"after:{date}").execute()
        return message_list.get("messages", [])

    def get_message_details(self, message_id):
        """
        주어진 메시지 ID에 대한 상세 정보를 가져옵니다.
        인자:
            message_id (str): Gmail 메시지의 ID.
        반환값:
            message (dict): 전체 메시지 상세 정보.
        """
        return self.service.users().messages().get(userId="me", id=message_id).execute()


class MessageHandler:
    @staticmethod
    def save_attachment(service, message_id, part, save_dir="downloaded_files"):
        """
        메시지 부분에서 첨부 파일을 다운로드하여 로컬 파일 시스템에 저장합니다.
        인자:
            service: Gmail API 서비스 객체.
            message_id (str): Gmail 메시지의 ID.
            part (dict): 첨부 파일을 포함하는 메시지 부분.
            save_dir (str): 첨부 파일을 저장할 디렉토리.
        """
        att_id = part["body"]["attachmentId"]
        att = service.users().messages().attachments().get(userId="me", messageId=message_id, id=att_id).execute()
        save_file(decode_base64(att["data"]), part["file"])

    @staticmethod
    def process_message_part(service, message_id: str, part: dict) -> str:
        """
        메시지 부분을 재귀적으로 처리하여 본문을 디코딩합니다.
        MIME 타입: text/plain 만 고려합니다.
        인자:
            service: Gmail API 서비스 객체.
            message_id (str): Gmail 메시지의 ID.
            part (dict): 메시지 부분.
        반환값:
            str: 메시지의 본문 텍스트.
        """
        # TODO: 다양한 MIME 타입에 대한 처리

        if part["mimeType"] == "text/plain":
            return decode_base64(part["body"]["data"])

        if part.get("filename"):
            MessageHandler.save_attachment(service, message_id, part)

        plain_text = ""
        if "multipart" in part["mimeType"]:
            for sub_part in part["parts"]:
                plain_text += MessageHandler.process_message_part(service, message_id, sub_part)

        return plain_text

    @staticmethod
    def process_message(service, message):
        """
        단일 메시지를 처리하고 본문을 디코딩합니다.
        인자:
            service: Gmail API 서비스 객체.
            message (dict): 전체 메시지 상세 정보.
        """
        payload = message.get("payload", {})
        body = MessageHandler.process_message_part(service, message["id"], payload)

        return body

    @staticmethod
    def process_headers(message) -> dict[str, str]:
        """
        메시지의 헤더를 딕셔너리로 파싱합니다.

        인자:
            message: Gmail 메시지 객체

        반환값:
            dict[str, str]: 파싱된 헤더 정보.
        """
        payload = message.get("payload", {})
        headers = payload.get("headers", [])
        return {
            "recipients": next((item["value"] for item in headers if item["name"] == "To"), None),
            "sender": next((item["value"] for item in headers if item["name"] == "From"), None),
            "cc": next((item["value"] for item in headers if item["name"] == "Cc"), ""),
            "subject": next((item["value"] for item in headers if item["name"] == "Subject"), None),
            "date": next((item["value"] for item in headers if item["name"] == "Date"), None),
        }
