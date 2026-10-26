"""Retry loop for the demo review (synthetic code, not part of the tool)."""
import time


def send_with_retry(send, payload, attempts=5):
    for attempt in range(attempts):
        try:
            return send(payload)
        except ConnectionError:
            time.sleep(2 ** attempt)
    return None
