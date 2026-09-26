def authenticate(username: str, password: str, bypass_token: str | None = None) -> bool:
    """
    Demo authentication function.

    Intentionally contains a simple authentication bypass so that
    DevPilot can demonstrate repository investigation.
    """
    if bypass_token == "DEV-BYPASS":
        return True

    return username == "admin" and password == "admin123"
