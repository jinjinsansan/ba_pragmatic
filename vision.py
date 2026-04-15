"""AI Vision — スクリーンショットを解析して座標を返す"""
import base64
import logging
import os

import anthropic

logger = logging.getLogger("baccarat.vision")

client = None


def _get_client():
    global client
    if client is None:
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return client


def analyze_screenshot(screenshot_bytes: bytes, instruction: str) -> dict:
    """スクリーンショットを解析し、指定された要素の座標を返す

    Args:
        screenshot_bytes: PNG画像のバイト列
        instruction: 何を探すかの指示

    Returns:
        {"x": int, "y": int, "description": str} または {"error": str}
    """
    c = _get_client()
    b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    response = c.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                },
                {
                    "type": "text",
                    "text": f"""This is a screenshot of an Evolution Gaming baccarat table in a browser.

{instruction}

IMPORTANT: Return ONLY a JSON object with the pixel coordinates of the CENTER of the element.
Format: {{"x": <number>, "y": <number>, "description": "<what you found>"}}
If the element is not found, return: {{"error": "not found", "description": "<what you see instead>"}}
Do not include any other text, only the JSON.""",
                },
            ],
        }],
    )

    text = response.content[0].text.strip()
    import json, re

    # JSON部分を抽出 ({...} を探す)
    match = re.search(r'\{[^{}]*\}', text)
    if not match:
        return {"error": "no json", "raw": text[:200]}

    raw = match.group()
    # まず標準JSON
    try:
        return json.loads(raw)
    except Exception:
        pass
    # シングルクォート → ダブルクォート
    try:
        fixed = raw.replace("'", '"')
        return json.loads(fixed)
    except Exception:
        pass
    # 正規表現で数値と文字列を抽出
    try:
        x_m = re.search(r'"?x"?\s*:\s*(\d+)', raw)
        y_m = re.search(r'"?y"?\s*:\s*(\d+)', raw)
        if x_m and y_m:
            x = int(x_m.group(1))
            y = int(y_m.group(1))
            desc_m = re.search(r'"?description"?\s*:\s*["\'](.+?)["\']', raw)
            desc = desc_m.group(1) if desc_m else ""
            return {"x": x, "y": y, "description": desc}
    except Exception:
        pass
    # yキー欠落: 2つの連続数値を探す (例: "x": 1035, 588)
    try:
        nums = re.findall(r'(\d{2,4})', raw)
        if len(nums) >= 2:
            x, y = int(nums[0]), int(nums[1])
            desc_m = re.search(r'"?description"?\s*:\s*["\'](.+?)["\']', raw)
            desc = desc_m.group(1) if desc_m else ""
            return {"x": x, "y": y, "description": desc}
    except Exception:
        pass
    # error キーを探す
    err_m = re.search(r'"?error"?\s*:\s*["\'](.+?)["\']', raw)
    if err_m:
        return {"error": err_m.group(1), "raw": raw[:200]}
    return {"error": "parse failed", "raw": raw[:200]}


def find_element(page, instruction: str) -> dict:
    """ページのスクリーンショットを撮って要素を探す"""
    screenshot = page.screenshot(type="png")
    return analyze_screenshot(screenshot, instruction)


def find_and_click(page, instruction: str) -> bool:
    """要素を見つけてクリック"""
    result = find_element(page, instruction)
    logger.info(f"Vision: {instruction} -> {result}")

    if "error" in result:
        logger.warning(f"Vision error: {result}")
        return False

    x = result.get("x", 0)
    y = result.get("y", 0)
    if x > 0 and y > 0:
        page.mouse.click(x, y)
        return True
    return False
