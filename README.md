# RevAI Anki Plugin - Modified Version

This is a personal, private backup of the RevAI Anki plugin with local modifications.

## Original

- Original plugin: RevAI - AI Buttons for Anki Review
- Source: AnkiWeb addon 1059061770

## Modifications Made

1. **Streaming output**: AI responses are now displayed token-by-token in the reviewer, similar to the RevAI website, instead of waiting for the full response.
2. **Markdown rendering during streaming**: Accumulated text is converted to HTML in real time so raw markdown symbols are not shown.
3. **Connection reuse**: `OpenRouterClient` and `BackendClient` now reuse a single opener across requests to reduce TLS handshake overhead for subsequent requests.
4. **WrongSpelling capture fix**: Improved typed-answer capture with DOM fallback for Anki's type-in-answer feature; WrongSpelling is cleared when the word is spelled correctly.
5. **Clear previous analysis**: Clicking the AI button now clears the previous analysis before generating a new one.
6. **Dual-prompt routing**: Actions can define separate prompts for "correct spelling" and "incorrect spelling". The plugin chooses the prompt before sending the request, so the model does not need to perform conditional reasoning.
7. **UI toggle**: Added a "Stream output token-by-token" option in RevAI Config > Model Config.

## Files Changed

- `__init__.py`
- `openrouter_client.py`
- `backend_client.py`
- `config_dialog.py`
- `config.json`
- `streaming_debug.py` (new)
- `prompt_template_correct.txt` / `prompt_template_incorrect.txt` (example prompts)

## Usage

1. Copy the contents of this repo into Anki's addon folder:
   ```
   %APPDATA%/Anki2/addons21/1059061770/
   ```
2. Restart Anki.

## Privacy / License Note

This repository is private and intended for personal backup only. The original RevAI plugin and its assets are the property of their respective author. This repo contains the original code plus local patches; it is not an official fork or redistribution.
