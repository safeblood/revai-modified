import json
import ssl
import socket
from urllib.request import urlopen, Request, build_opener, HTTPSHandler
from urllib.error import HTTPError, URLError

try:
    import certifi
    ssl_context = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    ssl_context = ssl.create_default_context()

SUPABASE_URL = "https://glxelrjvljzyextvpama.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImdseGVscmp2bGp6eWV4dHZwYW1hIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzUyNzc4ODYsImV4cCI6MjA5MDg1Mzg4Nn0.3wghJV5yWkUbDauahmzA8VN_Irvn8LL56Gw0KEcyW4o"

# Timeouts
AUTH_TIMEOUT = 15     # seconds for login/register/refresh
STATUS_TIMEOUT = 15   # seconds for user status check
GENERATE_TIMEOUT = 120  # seconds for AI generation (LLMs can be slow)
COUPON_TIMEOUT = 15   # seconds for coupon redemption


class AuthError(Exception):
    """Raised when authentication fails."""
    pass


class CreditsExhaustedError(Exception):
    """Raised when user has no credits remaining."""
    pass


class NetworkError(Exception):
    """Raised on connection/timeout issues."""
    pass


class BackendClient:
    """Client for the RevAI Supabase backend."""

    def __init__(self, access_token=None, refresh_token=None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        # Reuse a single opener across requests to keep TCP/TLS connections alive
        # when the server supports HTTP keep-alive.
        self._opener = build_opener(HTTPSHandler(context=ssl_context))

    # --- Supabase Auth (GoTrue REST API) ---

    def register(self, email, password):
        """Register a new user. Returns (access_token, refresh_token, email)."""
        data = {"email": email, "password": password}
        result = self._auth_request("signup", data)

        session = result.get("session") or {}
        if not session.get("access_token"):
            raise AuthError(
                "Registration successful! Check your email to confirm your account, "
                "then sign in."
            )

        self.access_token = session["access_token"]
        self.refresh_token = session["refresh_token"]
        user_email = result.get("user", {}).get("email", email)
        return self.access_token, self.refresh_token, user_email

    def login(self, email, password):
        """Login an existing user. Returns (access_token, refresh_token, email)."""
        data = {
            "email": email,
            "password": password,
        }
        result = self._auth_request("token?grant_type=password", data)

        self.access_token = result["access_token"]
        self.refresh_token = result["refresh_token"]
        user_email = result.get("user", {}).get("email", email)
        return self.access_token, self.refresh_token, user_email

    def refresh_access_token(self):
        """Refresh an expired access token."""
        if not self.refresh_token:
            raise AuthError("No refresh token available. Please log in again.")

        data = {"refresh_token": self.refresh_token}
        result = self._auth_request("token?grant_type=refresh_token", data)

        self.access_token = result["access_token"]
        self.refresh_token = result["refresh_token"]
        return self.access_token, self.refresh_token

    def _auth_request(self, endpoint, data):
        """Make a Supabase Auth (GoTrue) REST API request."""
        req = Request(
            f"{SUPABASE_URL}/auth/v1/{endpoint}",
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "apikey": SUPABASE_ANON_KEY,
            },
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=AUTH_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except socket.timeout:
            raise NetworkError(
                "Connection timed out. Check your internet and try again."
            )
        except HTTPError as e:
            try:
                error_body = e.read().decode("utf-8")
                error_data = json.loads(error_body)
                error_msg = (
                    error_data.get("error_description")
                    or error_data.get("msg")
                    or error_data.get("error", "")
                )
            except Exception:
                error_msg = str(e)

            msg_lower = error_msg.lower()
            if "already registered" in msg_lower or "already been registered" in msg_lower:
                raise AuthError("An account with this email already exists.")
            elif "invalid login" in msg_lower or "invalid email or password" in msg_lower:
                raise AuthError("Incorrect email or password.")
            elif "email not confirmed" in msg_lower:
                raise AuthError("Please confirm your email address first.")
            elif "rate limit" in msg_lower:
                raise AuthError("Too many attempts. Try again later.")
            elif e.code >= 500:
                raise NetworkError("Server error. Please try again later.")
            else:
                raise AuthError(f"Authentication error: {error_msg}")
        except URLError as e:
            if "timed out" in str(e.reason).lower():
                raise NetworkError("Connection timed out. Check your internet.")
            raise NetworkError(f"Cannot connect to server: {e.reason}")

    # --- Backend API calls (Edge Functions) ---

    def redeem_coupon(self, code):
        """Redeem a coupon code. Returns (message, new_credits)."""
        result = self._backend_request(
            "POST",
            f"{SUPABASE_URL}/functions/v1/redeem-coupon",
            {"code": code},
            timeout=COUPON_TIMEOUT,
        )
        return result.get("message", "Coupon redeemed!"), result.get("credits", 0)

    def generate(self, prompt_text, model=None):
        """Send a generation request through the backend proxy.
        Returns (generated_text, meta_dict).
        """
        data = {
            "messages": [{"role": "user", "content": prompt_text}],
        }
        if model:
            data["model"] = model

        result = self._backend_request(
            "POST",
            f"{SUPABASE_URL}/functions/v1/generate",
            data,
            timeout=GENERATE_TIMEOUT,
        )

        if result and result.get("choices"):
            content = result["choices"][0].get("message", {}).get("content")
            if content is not None:
                return content.strip(), result.get("_reviewai", {})
        raise Exception(
            "AI model returned an empty response. Try again or switch to a different model."
        )

    def generate_stream(self, prompt_text, on_token, model=None):
        """Stream a generation request through the backend proxy.

        Calls on_token(token) for each chunk and returns (full_text, meta_dict).
        If the backend does not support streaming, falls back to the normal
        JSON response and emits the full text as a single token.
        """
        if not self.access_token:
            raise AuthError("Not logged in. Please log in first.")

        data = {
            "messages": [{"role": "user", "content": prompt_text}],
            "stream": True,
        }
        if model:
            data["model"] = model

        url = f"{SUPABASE_URL}/functions/v1/generate"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "apikey": SUPABASE_ANON_KEY,
        }
        req = Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            response = self._opener.open(req, timeout=GENERATE_TIMEOUT)
        except socket.timeout:
            raise NetworkError(
                "Request timed out. The AI model may be overloaded. Try again."
            )
        except HTTPError as e:
            if e.code == 401:
                raise AuthError("Session expired. Please log in again.")
            elif e.code == 402:
                try:
                    error_data = json.loads(e.read().decode("utf-8"))
                    raise CreditsExhaustedError(
                        error_data.get("hint", "No credits remaining.")
                    )
                except (json.JSONDecodeError, AttributeError):
                    raise CreditsExhaustedError("No credits remaining.")
            elif e.code == 429:
                raise NetworkError("Rate limited. Wait a moment and try again.")
            elif e.code >= 500:
                raise NetworkError(f"Server error ({e.code}). Please try again later.")
            else:
                try:
                    error_body = e.read().decode("utf-8")
                    msg = json.loads(error_body).get("error", str(e))
                except Exception:
                    msg = f"HTTP Error {e.code}"
                raise Exception(f"Backend error: {msg}") from e
        except URLError as e:
            if "timed out" in str(e.reason).lower():
                raise NetworkError("Connection timed out. Check your internet.")
            raise NetworkError(f"Cannot connect to server: {e.reason}")

        full_text = ""
        meta = {}
        try:
            with response:
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" not in content_type.lower():
                    body = response.read().decode("utf-8")
                    if body:
                        try:
                            result = json.loads(body)
                            if result and result.get("choices"):
                                content = result["choices"][0].get("message", {}).get("content")
                                if content is not None:
                                    full_text = content.strip()
                                    on_token(full_text)
                            meta = result.get("_reviewai", {})
                        except json.JSONDecodeError:
                            pass
                    return full_text, meta

                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        full_text += content
                        on_token(content)
                    if choices[0].get("finish_reason"):
                        break
                    # Some backends embed metadata in the final chunk.
                    if "_reviewai" in chunk:
                        meta = chunk["_reviewai"]
        except socket.timeout:
            raise NetworkError(
                "Request timed out while streaming. The model stopped responding."
            )

        return full_text.strip(), meta

    def get_user_status(self):
        """Get current user's credits and tier."""
        return self._backend_request(
            "GET",
            f"{SUPABASE_URL}/functions/v1/user-status",
            timeout=STATUS_TIMEOUT,
        )

    def _backend_request(self, method, url, data=None, timeout=GENERATE_TIMEOUT, _retried=False):
        """Make an authenticated request to the backend."""
        if not self.access_token:
            raise AuthError("Not logged in. Please log in first.")

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "apikey": SUPABASE_ANON_KEY,
        }
        req = Request(
            url,
            data=json.dumps(data).encode("utf-8") if data else None,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except socket.timeout:
            raise NetworkError(
                "Request timed out. The AI model may be overloaded. Try again."
            )
        except HTTPError as e:
            if e.code == 401 and not _retried:
                try:
                    self.refresh_access_token()
                    return self._backend_request(method, url, data, timeout, _retried=True)
                except (AuthError, NetworkError):
                    raise AuthError("Session expired. Please log in again.")
            elif e.code == 402:
                try:
                    error_data = json.loads(e.read().decode("utf-8"))
                    raise CreditsExhaustedError(
                        error_data.get("hint", "No credits remaining.")
                    )
                except (json.JSONDecodeError, AttributeError):
                    raise CreditsExhaustedError("No credits remaining.")
            elif e.code == 429:
                raise NetworkError("Rate limited. Wait a moment and try again.")
            elif e.code >= 500:
                raise NetworkError(f"Server error ({e.code}). Please try again later.")
            else:
                try:
                    error_body = e.read().decode("utf-8")
                    msg = json.loads(error_body).get("error", str(e))
                except Exception:
                    msg = f"HTTP Error {e.code}"
                raise Exception(f"Backend error: {msg}") from e
        except URLError as e:
            if "timed out" in str(e.reason).lower():
                raise NetworkError("Connection timed out. Check your internet.")
            raise NetworkError(f"Cannot connect to server: {e.reason}")
