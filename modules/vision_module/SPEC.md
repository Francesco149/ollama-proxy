# vision_module

## Purpose
Translates Ollama-style message payloads (base64 images in `msg["images"]`) into OpenAI-compatible multipart content arrays.

## Exports
```python
def to_openai_messages(messages: list, images: list = None) -> list
```

## Imports From
None — no internal dependencies.

## Behavior Rules
- Images in `msg["images"]` are extracted per-message
- Top-level `images` param is appended only to the last user message
- Images not prefixed with `data:` are wrapped as `data:image/jpeg;base64,<img>`
- Output per image-bearing message: `[{"type": "text", "text": ...}, {"type": "image_url", "image_url": {"url": ...}}, ...]`
- Messages without images pass through as `{"role": ..., "content": <str>}`

## Must NOT
- Import from any other internal module
- Perform any I/O or HTTP calls
- Mutate the input `messages` list
