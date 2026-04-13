def to_openai_messages(messages: list, images: list = None) -> list:
    result = []
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        msg_images = list(msg.get("images", []))
        if images and i == len(messages)-1 and msg["role"] == "user":
            msg_images += images
        if msg_images:
            parts = [{"type": "text", "text": content}]
            for img in msg_images:
                if not img.startswith("data:"):
                    img = f"data:image/jpeg;base64,{img}"
                parts.append({"type": "image_url", "image_url": {"url": img}})
            result.append({"role": msg["role"], "content": parts})
        else:
            result.append({"role": msg["role"], "content": content})
    return result
