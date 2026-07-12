import json
import ssl
import socket
from urllib.request import urlopen, Request, build_opener, ProxyHandler, HTTPSHandler
from urllib.error import HTTPError, URLError

from .streaming_debug import log_debug

try:
    import certifi
    ssl_context = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    ssl_context = ssl.create_default_context()

# Timeouts
CONNECT_TIMEOUT = 10  # seconds to establish connection
READ_TIMEOUT = 120    # seconds to wait for LLM response (can be slow)
MODELS_TIMEOUT = 15   # seconds for fetching model list


class OpenRouterClient:
    """OpenAI-compatible client for OpenRouter or any custom endpoint."""

    DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self, api_key, base_url=None, no_proxy=False):
        self.api_key = api_key
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.no_proxy = no_proxy
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

        # Reuse a single opener across requests to keep TCP/TLS connections alive
        # when the server supports HTTP keep-alive.
        if self.no_proxy:
            self._opener = build_opener(
                ProxyHandler({}),
                HTTPSHandler(context=ssl_context),
            )
        else:
            self._opener = build_opener(HTTPSHandler(context=ssl_context))

    def _request(self, method, endpoint, data=None, timeout=READ_TIMEOUT):
        req = Request(
            f"{self.base_url}/{endpoint}",
            data=json.dumps(data).encode("utf-8") if data else None,
            headers=self.headers,
            method=method,
        )
        try:
            response = self._opener.open(req, timeout=timeout)
            with response:
                if response.status == 204:
                    return None
                body = response.read().decode("utf-8")
                if not body:
                    return {} if 200 <= response.status < 300 else None
                return json.loads(body)
        except socket.timeout:
            raise Exception(
                "Request timed out. The AI model is taking too long to respond. "
                "Try again or switch to a faster model."
            )
        except HTTPError as e:
            raw_body = ""
            try:
                raw_body = e.read().decode("utf-8")
                error_json = json.loads(raw_body)
                err = error_json.get("error", "")
                if isinstance(err, dict):
                    msg = err.get("message", "")
                elif isinstance(err, str):
                    msg = err
                else:
                    msg = str(err)
                if not msg:
                    msg = error_json.get("message", "")
            except Exception:
                msg = raw_body.strip() if raw_body else f"HTTP Error {e.code}"

            is_openrouter = "openrouter.ai" in self.base_url
            if e.code == 401:
                if is_openrouter:
                    raise Exception("Invalid OpenRouter API key. Check your key in RevAI Config.")
                raise Exception("Invalid API key or authentication failed. Check your Direct API key.")
            elif e.code == 402:
                if is_openrouter:
                    raise Exception("OpenRouter account has insufficient credits.")
                raise Exception(f"API Error ({e.code}): {msg}")
            elif e.code == 429:
                raise Exception("Rate limited. Wait a moment and try again.")
            elif e.code >= 500:
                raise Exception(f"Server error ({e.code}). Try again later.")
            else:
                raise Exception(f"API Error ({e.code}): {msg}") from e
        except URLError as e:
            if "timed out" in str(e.reason).lower():
                raise Exception(
                    "Connection timed out. Check that the API server is running."
                )
            raise Exception(f"Network error: {e.reason}") from e

    def get_models(self):
        try:
            response = self._request("GET", "models", timeout=MODELS_TIMEOUT)
            return response.get("data", []) if response else []
        except Exception:
            return []

    def generate(self, model_name, prompt_text):
        if not model_name:
            raise ValueError("No model selected. Go to RevAI Config > Model Config.")
        if not prompt_text:
            raise ValueError("Prompt is empty. Check your action's prompt template.")

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt_text}],
        }
        response = self._request("POST", "chat/completions", data=payload)
        if response and response.get("choices"):
            content = response["choices"][0].get("message", {}).get("content")
            if content is not None:
                return content.strip()
        raise Exception(
            "AI model returned an empty response. Try again or switch to a different model."
        )

    def generate_stream(self, model_name, prompt_text, on_token):
        """Stream tokens from an OpenAI-compatible chat completions endpoint.

        Calls on_token(token) for each delta.content chunk and returns the
        accumulated full text. Falls back to non-streaming behaviour if the
        endpoint ignores the stream flag.
        """
        if not model_name:
            raise ValueError("No model selected. Go to RevAI Config > Model Config.")
        if not prompt_text:
            raise ValueError("Prompt is empty. Check your action's prompt template.")

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt_text}],
            "stream": True,
        }
        log_debug(f"generate_stream: base_url={self.base_url}, model={model_name}, prompt_len={len(prompt_text)}, payload_len={len(json.dumps(payload))}")
        req = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self.headers,
            method="POST",
        )

        start_time = __import__("time").time()
        try:
            response = self._opener.open(req, timeout=READ_TIMEOUT)
        except socket.timeout:
            raise Exception(
                "Request timed out. The AI model is taking too long to respond. "
                "Try again or switch to a faster model."
            )
        except HTTPError as e:
            raw_body = ""
            try:
                raw_body = e.read().decode("utf-8")
                error_json = json.loads(raw_body)
                err = error_json.get("error", "")
                if isinstance(err, dict):
                    msg = err.get("message", "")
                elif isinstance(err, str):
                    msg = err
                else:
                    msg = str(err)
                if not msg:
                    msg = error_json.get("message", "")
            except Exception:
                msg = raw_body.strip() if raw_body else f"HTTP Error {e.code}"

            is_openrouter = "openrouter.ai" in self.base_url
            if e.code == 401:
                if is_openrouter:
                    raise Exception("Invalid OpenRouter API key. Check your key in RevAI Config.")
                raise Exception("Invalid API key or authentication failed. Check your Direct API key.")
            elif e.code == 402:
                if is_openrouter:
                    raise Exception("OpenRouter account has insufficient credits.")
                raise Exception(f"API Error ({e.code}): {msg}")
            elif e.code == 429:
                raise Exception("Rate limited. Wait a moment and try again.")
            elif e.code >= 500:
                raise Exception(f"Server error ({e.code}). Try again later.")
            else:
                raise Exception(f"API Error ({e.code}): {msg}") from e
        except URLError as e:
            if "timed out" in str(e.reason).lower():
                raise Exception(
                    "Connection timed out. Check that the API server is running."
                )
            raise Exception(f"Network error: {e.reason}") from e

        full_text = ""
        try:
            with response:
                # Some endpoints ignore stream=True and return a normal JSON body.
                content_type = response.headers.get("Content-Type", "")
                log_debug(f"generate_stream: status={response.status}, content-type={content_type}")
                log_debug(f"generate_stream: starting SSE read loop")
                first_line_time = None
                line_count = 0
                if "text/event-stream" not in content_type.lower():
                    log_debug("generate_stream: non-SSE response, falling back to JSON parse")
                    body = response.read().decode("utf-8")
                    if body:
                        try:
                            data = json.loads(body)
                            if data and data.get("choices"):
                                content = data["choices"][0].get("message", {}).get("content")
                                if content is not None:
                                    full_text = content.strip()
                                    on_token(full_text)
                                    log_debug(f"generate_stream: fallback emitted full text, len={len(full_text)}")
                        except json.JSONDecodeError:
                            pass
                    return full_text

                token_count = 0
                while True:
                    raw_line = response.readline()
                    if not raw_line:
                        log_debug("generate_stream: readline returned empty, breaking")
                        break
                    line_count += 1
                    now = __import__("time").time()
                    if first_line_time is None:
                        first_line_time = now
                        log_debug(f"generate_stream: first line received after {first_line_time - start_time:.2f}s")
                    if line_count <= 5:
                        preview = raw_line.decode("utf-8", errors="replace").strip()[:120]
                        log_debug(f"generate_stream: line #{line_count} after {now - start_time:.2f}s: {preview}")
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        log_debug("generate_stream: received [DONE]")
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
                        if token_count == 0:
                            log_debug(f"generate_stream: first content after {now - start_time:.2f}s, content={content!r}")
                        full_text += content
                        on_token(content)
                        token_count += 1
                    if choices[0].get("finish_reason"):
                        log_debug(f"generate_stream: finish_reason={choices[0].get('finish_reason')}")
                        break
                log_debug(f"generate_stream: total lines={line_count}, tokens={token_count}, full_len={len(full_text)}")
        except socket.timeout:
            raise Exception(
                "Request timed out while streaming. The model stopped responding. "
                "Try again or switch to a faster model."
            )

        return full_text.strip()
