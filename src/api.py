import json


def parse_request(body: str) -> dict:
    return json.loads(body)
